#!/usr/bin/env python3
"""
GOR Episodic Memory — /recall command handler.

Called by Claude (Sonnet/Opus) when user types /recall.
Reads last surfaced entities from DB, applies feedback.

Usage (called by Claude, not directly):
  /recall good           → +1 all entities from last recall
  /recall miss           → -1 all entities from last recall
  /recall <free text>    → Claude interprets and applies targeted feedback

This module provides the DB operations. Claude handles the interpretation.
"""

import json
import os
import sqlite3
import sys
from pathlib import Path


def get_db():
    config_path = Path(__file__).parent / "config.json"
    config = json.loads(config_path.read_text())
    db_path = os.path.expanduser(config.get("db_path", "~/.engram/memory.db"))
    conn = sqlite3.connect(db_path, timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def get_last_surfaced_entities(session_id: str = None) -> list[dict]:
    """Get the most recently surfaced entities, optionally filtered by session."""
    conn = get_db()
    if session_id:
        rows = conn.execute(
            """SELECT entity, surfaced_at, message_index
               FROM last_surfaced WHERE session_id = ?
               ORDER BY surfaced_at DESC""",
            (session_id,),
        ).fetchall()
    else:
        # Get from most recent session
        rows = conn.execute(
            """SELECT entity, surfaced_at, message_index
               FROM last_surfaced
               ORDER BY surfaced_at DESC LIMIT 10""",
        ).fetchall()
    conn.close()
    return [{"entity": r[0], "surfaced_at": r[1], "messages_ago": r[2]} for r in rows]


def apply_feedback(
    session_id: str,
    entity: str,
    score: int,
    user_note: str = None,
    reasoning: str = None,
) -> dict:
    """Apply explicit feedback to an entity's recall score."""
    conn = get_db()
    conn.execute(
        """INSERT INTO recall_feedback (session_id, entity, score, source, user_note, reasoning)
           VALUES (?, ?, ?, 'explicit', ?, ?)""",
        (session_id, entity, score, user_note, reasoning),
    )
    conn.commit()

    # Return current total feedback for this entity
    total = conn.execute(
        "SELECT COALESCE(SUM(score), 0) FROM recall_feedback WHERE entity = ?",
        (entity,),
    ).fetchone()[0]
    conn.close()
    return {"entity": entity, "applied": score, "total_feedback": total}


def get_entity_stats(entity: str) -> dict:
    """Get full stats for an entity — mentions, sessions, feedback."""
    conn = get_db()
    mentions = conn.execute(
        "SELECT COUNT(*) FROM mentions WHERE entity = ?", (entity,)
    ).fetchone()[0]
    sessions = conn.execute(
        "SELECT COUNT(DISTINCT session_id) FROM mentions WHERE entity = ?", (entity,)
    ).fetchone()[0]
    feedback = conn.execute(
        "SELECT COALESCE(SUM(score), 0) FROM recall_feedback WHERE entity = ?", (entity,)
    ).fetchone()[0]
    recent_snippets = conn.execute(
        """SELECT session_id, context_snippet, ts FROM mentions
           WHERE entity = ? ORDER BY ts DESC LIMIT 3""",
        (entity,),
    ).fetchall()
    conn.close()
    return {
        "entity": entity,
        "total_mentions": mentions,
        "total_sessions": sessions,
        "feedback_score": feedback,
        "recent_snippets": [
            {"session": r[0], "snippet": r[1][:120] if r[1] else "", "ts": r[2]}
            for r in recent_snippets
        ],
    }


if __name__ == "__main__":
    # CLI mode for testing
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["last", "stats", "boost", "penalize"])
    parser.add_argument("--entity", "-e", help="Entity name")
    parser.add_argument("--session", "-s", help="Session ID")
    parser.add_argument("--note", "-n", help="User note")
    args = parser.parse_args()

    if args.action == "last":
        for e in get_last_surfaced_entities(args.session):
            print(f"  {e['entity']:30s} surfaced={e['surfaced_at']}  msgs_ago={e['messages_ago']}")
    elif args.action == "stats":
        if not args.entity:
            print("--entity required")
            sys.exit(1)
        stats = get_entity_stats(args.entity)
        print(json.dumps(stats, indent=2))
    elif args.action in ("boost", "penalize"):
        if not args.entity or not args.session:
            print("--entity and --session required")
            sys.exit(1)
        score = +1 if args.action == "boost" else -1
        result = apply_feedback(args.session, args.entity, score, user_note=args.note)
        print(json.dumps(result, indent=2))
