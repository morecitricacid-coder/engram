-- Migration 005: Track compression level per mention
-- Enables background densification of snippets.

ALTER TABLE mentions ADD COLUMN compression_level TEXT DEFAULT 'none';

CREATE INDEX IF NOT EXISTS idx_mentions_compression ON mentions(compression_level);

INSERT INTO schema_version VALUES (5, datetime('now'), 'Add compression_level to mentions');
