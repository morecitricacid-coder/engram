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
    try:
        conn.execute(
            "INSERT INTO mentions (session_id, speaker, entity, raw_text, context_snippet, source, compression_level) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (session_id, speaker, entity, raw_text, context_snippet, source, compression_level))
        conn.commit()
    except sqlite3.IntegrityError:
        pass


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
