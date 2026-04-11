-- Migration 009: Normalized snippet storage (P3a)
--
-- Problem: When a message mentions N entities, the same snippet is stored N times
-- in mentions.context_snippet. 75% of all snippet data is exact duplicates.
--
-- Solution: Store each unique snippet ONCE in snippet_store, keyed by hash.
-- mentions.snippet_id references it. The trigger and queries JOIN through it.
--
-- Backward compat: Old rows keep context_snippet populated. New rows set it NULL
-- and use snippet_id. All reads use COALESCE(snippet_store.content, context_snippet).

-- 1. Create normalized snippet store
CREATE TABLE IF NOT EXISTS snippet_store (
    hash TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    compression_level TEXT DEFAULT 'none',
    created_at TEXT DEFAULT (datetime('now'))
);

-- 2. Backfill unique snippets from existing mentions
INSERT OR IGNORE INTO snippet_store (hash, content, compression_level)
SELECT DISTINCT snippet_hash, context_snippet, compression_level
FROM mentions
WHERE snippet_hash IS NOT NULL
  AND context_snippet IS NOT NULL
  AND context_snippet != '';

-- 3. Add snippet_id FK column (references snippet_store.hash)
ALTER TABLE mentions ADD COLUMN snippet_id TEXT;
UPDATE mentions SET snippet_id = snippet_hash WHERE snippet_hash IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_mentions_snippet_id ON mentions(snippet_id);

-- 4. Rewrite surface trigger to resolve snippets through snippet_store
--    COALESCE handles transition: prefers snippet_store, falls back to inline
DROP TRIGGER IF EXISTS trg_mention_surface;

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
                -- Prefer snippet_store for the best user-speaker mention
                (SELECT COALESCE(ss.content, m2.context_snippet)
                 FROM mentions m2
                 LEFT JOIN snippet_store ss ON ss.hash = m2.snippet_id
                 WHERE m2.session_id = m.session_id
                   AND m2.entity = NEW.entity
                   AND m2.speaker = 'user'
                   AND (m2.snippet_id IS NOT NULL OR m2.context_snippet IS NOT NULL)
                 ORDER BY m2.ts ASC
                 LIMIT 1),
                -- Fallback: any mention
                (SELECT COALESCE(ss.content, m2.context_snippet)
                 FROM mentions m2
                 LEFT JOIN snippet_store ss ON ss.hash = m2.snippet_id
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

INSERT INTO schema_version (version, description)
VALUES (9, 'Normalized snippet_store, trigger uses JOIN');
