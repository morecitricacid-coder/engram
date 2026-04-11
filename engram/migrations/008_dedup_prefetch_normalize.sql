-- Migration 008: Snippet dedup + predictive prefetch + normalization index
--
-- P0: snippet_hash column for O(1) duplicate snippet detection at write time.
-- P1: transition_probs table for precomputed entity co-occurrence probabilities.
-- P2: index on entity_aliases for fast normalization lookups in parser.

-- P0: Snippet dedup
ALTER TABLE mentions ADD COLUMN snippet_hash TEXT;
UPDATE mentions SET snippet_hash = substr(context_snippet, 1, 50)
    WHERE context_snippet IS NOT NULL AND context_snippet != '';
CREATE INDEX IF NOT EXISTS idx_mentions_dedup ON mentions(entity, snippet_hash);

-- P1: Predictive prefetch — precomputed transition probabilities
CREATE TABLE IF NOT EXISTS transition_probs (
    from_entity TEXT NOT NULL,
    to_entity TEXT NOT NULL,
    probability REAL NOT NULL,
    shared_sessions INTEGER NOT NULL DEFAULT 0,
    from_sessions INTEGER NOT NULL DEFAULT 0,
    computed_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (from_entity, to_entity)
);
CREATE INDEX IF NOT EXISTS idx_tp_lookup ON transition_probs(from_entity, probability DESC);

-- P2: Fast alias resolution during entity normalization
CREATE INDEX IF NOT EXISTS idx_alias_lookup ON entity_aliases(alias);

INSERT INTO schema_version (version, description)
VALUES (8, 'Snippet dedup hash, transition probs, alias index');
