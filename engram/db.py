"""
Engram — Database operations.

Handles schema init, mention writes, connection queries, surface reads.
All operations are synchronous SQLite. Schema versioning via migrations/.
"""

import sqlite3
import os
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def get_db_path(config: dict) -> str:
    return os.path.expanduser(config.get("db_path", "~/.engram/memory.db"))


def init_db(config: dict) -> sqlite3.Connection:
    db_path = get_db_path(config)
    os.makedirs(os.path.dirname(db_path), exist_ok=True)

    conn = sqlite3.connect(db_path, timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA auto_vacuum=INCREMENTAL")

    try:
        row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
        current_version = row[0] if row and row[0] else 0
    except sqlite3.OperationalError:
        current_version = 0

    for sql_file in sorted(MIGRATIONS_DIR.glob("*.sql")):
        version = int(sql_file.name.split("_")[0])
        if version > current_version:
            conn.executescript(sql_file.read_text())

    return conn


def ensure_session(conn: sqlite3.Connection, session_id: str, model: str = None):
    conn.execute("INSERT OR IGNORE INTO sessions (id, model) VALUES (?, ?)", (session_id, model))
    conn.execute("UPDATE sessions SET message_count = message_count + 1 WHERE id = ?", (session_id,))
    conn.commit()


def write_mention(conn, session_id, speaker, entity, raw_text=None, context_snippet=None, source="hook", compression_level="none"):
    snippet_hash = context_snippet[:50] if context_snippet else None
    try:
        # P3a: Write snippet to normalized store first (INSERT OR IGNORE = free dedup)
        if snippet_hash and context_snippet:
            conn.execute(
                "INSERT OR IGNORE INTO snippet_store (hash, content, compression_level) VALUES (?, ?, ?)",
                (snippet_hash, context_snippet, compression_level))
        # Mention references snippet_store via snippet_id; context_snippet=NULL for new rows
        conn.execute(
            "INSERT INTO mentions (session_id, speaker, entity, raw_text, context_snippet, source, compression_level, snippet_hash, snippet_id) "
            "VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)",
            (session_id, speaker, entity, raw_text, source, compression_level, snippet_hash, snippet_hash))
        conn.commit()
    except sqlite3.IntegrityError:
        pass


def is_duplicate_snippet(conn, entity, snippet, prefix_len=50):
    """Check if entity already has this snippet stored (prefix match via indexed hash)."""
    if not snippet:
        return False
    prefix = snippet[:prefix_len]
    row = conn.execute(
        "SELECT 1 FROM mentions WHERE entity=? AND snippet_hash=? LIMIT 1",
        (entity, prefix)
    ).fetchone()
    return row is not None


def get_unsurfaced(conn: sqlite3.Connection, session_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT id, recall_text FROM surface_queue WHERE session_id = ? AND surfaced = 0 ORDER BY created_at",
        (session_id,)).fetchall()
    if not rows:
        return []
    ids = [r[0] for r in rows]
    texts = [r[1] for r in rows]
    conn.execute(f"UPDATE surface_queue SET surfaced = 1 WHERE id IN ({','.join('?' * len(ids))})", ids)
    conn.commit()
    return texts


def get_entity_feedback(conn: sqlite3.Connection, entity: str) -> int:
    row = conn.execute("SELECT COALESCE(SUM(score), 0) FROM recall_feedback WHERE entity = ?", (entity,)).fetchone()
    return row[0]


def write_feedback(conn, session_id, entity, score, source="explicit", user_note=None, reasoning=None):
    conn.execute(
        "INSERT INTO recall_feedback (session_id, entity, score, source, user_note, reasoning) VALUES (?, ?, ?, ?, ?, ?)",
        (session_id, entity, score, source, user_note, reasoning))
    conn.commit()


def update_last_surfaced(conn: sqlite3.Connection, session_id: str, entities: list[str]):
    for entity in entities:
        conn.execute(
            "INSERT OR REPLACE INTO last_surfaced (session_id, entity, surfaced_at, message_index) VALUES (?, ?, datetime('now'), 0)",
            (session_id, entity))
    conn.commit()


def get_last_surfaced(conn: sqlite3.Connection, session_id: str) -> list[tuple[str, int]]:
    return [(r[0], r[1]) for r in conn.execute(
        "SELECT entity, message_index FROM last_surfaced WHERE session_id = ?", (session_id,)).fetchall()]


def increment_surfaced_message_index(conn: sqlite3.Connection, session_id: str):
    conn.execute("UPDATE last_surfaced SET message_index = message_index + 1 WHERE session_id = ?", (session_id,))
    conn.commit()


def get_session_transcript_path(conn: sqlite3.Connection, session_id: str) -> str | None:
    row = conn.execute("SELECT transcript_path FROM sessions WHERE id = ?", (session_id,)).fetchone()
    return row[0] if row else None


def set_session_transcript_path(conn: sqlite3.Connection, session_id: str, path: str):
    conn.execute("UPDATE sessions SET transcript_path = ? WHERE id = ?", (path, session_id))
    conn.commit()


# --- P1: Predictive prefetch ---

def get_prefetch_predictions(conn, current_entities, min_score=0.25, max_results=3):
    """Get predicted next-entities from precomputed transition probabilities.

    Only returns predictions with aggregate score >= min_score and that
    aren't already in the current entity set. Fast: single indexed query.
    """
    if not current_entities:
        return []
    placeholders = ",".join("?" * len(current_entities))
    rows = conn.execute(f"""
        SELECT to_entity, SUM(probability) as score, MAX(shared_sessions) as evidence
        FROM transition_probs
        WHERE from_entity IN ({placeholders})
          AND to_entity NOT IN ({placeholders})
        GROUP BY to_entity
        HAVING score >= ?
        ORDER BY score DESC
        LIMIT ?
    """, (*current_entities, *current_entities, min_score, max_results)).fetchall()
    return [(row[0], row[1], row[2]) for row in rows]


def rebuild_transition_probs(conn, min_entity_sessions=3, min_prob=0.05):
    """Recompute transition probability table from session co-occurrence.

    P(B|A) = sessions_with_both(A,B) / sessions_with(A)
    Only stores transitions with P >= min_prob to keep table small.
    Returns number of transitions stored.
    """
    from collections import defaultdict

    conn.execute("DELETE FROM transition_probs")

    rows = conn.execute("""
        SELECT session_id, entity FROM mentions
        WHERE entity IN (
            SELECT entity FROM mentions GROUP BY entity HAVING COUNT(DISTINCT session_id) >= ?
        )
    """, (min_entity_sessions,)).fetchall()

    session_entities = defaultdict(set)
    for sid, entity in rows:
        session_entities[sid].add(entity)

    entity_session_count = defaultdict(int)
    for entities in session_entities.values():
        for e in entities:
            entity_session_count[e] += 1

    cooccur = defaultdict(lambda: defaultdict(int))
    for entities in session_entities.values():
        for a in entities:
            for b in entities:
                if a != b:
                    cooccur[a][b] += 1

    batch = []
    for a, targets in cooccur.items():
        a_total = entity_session_count[a]
        if a_total == 0:
            continue
        for b, shared in targets.items():
            prob = shared / a_total
            if prob >= min_prob:
                batch.append((a, b, prob, shared, a_total))

    conn.executemany(
        "INSERT INTO transition_probs (from_entity, to_entity, probability, shared_sessions, from_sessions) VALUES (?, ?, ?, ?, ?)",
        batch
    )
    conn.commit()
    return len(batch)


# --- P2: Entity normalization ---

def normalize_entity_db(conn, entity):
    """Look up canonical name for an entity via entity_aliases table."""
    row = conn.execute(
        "SELECT canonical FROM entity_aliases WHERE alias=? LIMIT 1",
        (entity,)
    ).fetchone()
    return row[0] if row else entity


def write_entity_alias(conn, canonical, alias):
    """Store an entity alias mapping."""
    conn.execute(
        "INSERT OR REPLACE INTO entity_aliases (canonical, alias) VALUES (?, ?)",
        (canonical, alias)
    )
    conn.commit()


def get_all_aliases(conn):
    """Get all stored entity aliases as {alias: canonical} dict."""
    rows = conn.execute("SELECT alias, canonical FROM entity_aliases").fetchall()
    return {r[0]: r[1] for r in rows}
