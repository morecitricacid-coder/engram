-- Migration 003: Unique constraint on connections

CREATE UNIQUE INDEX IF NOT EXISTS idx_connections_unique
ON connections(mention_id, related_mention_id, entity);

INSERT INTO schema_version (version, description)
VALUES (3, 'Unique constraint on connections table');
