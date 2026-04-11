#!/usr/bin/env python3
"""
Engram session archiver — compress conversation transcripts for deep recall.

Reads Claude Code JSONL transcripts, extracts user+assistant messages,
Strix-compresses them, and stores in session_archives for on-demand recall.

Architecture:
  - Snippets (mentions table) = surface recall, the "> date: ..." lines
  - Archives (session_archives) = full conversation depth, loaded on demand

Usage:
  python3 -m engram.archive              # Archive all unarchived sessions
  python3 -m engram.archive --dry-run    # Show what would be archived
  python3 -m engram.archive --limit 10   # Archive at most 10 sessions
  python3 -m engram.archive --read <id>  # Read a compressed archive
"""

import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

try:
    from strix.compress import compress_deterministic, _sanitize_v11, _post_compress
    STRIX_AVAILABLE = True
except ImportError:
    STRIX_AVAILABLE = False


def _get_config():
    config_path = Path(__file__).parent / "config.json"
    if not config_path.exists():
        config_path = Path(__file__).parent / "config.example.json"
    return json.loads(config_path.read_text())


def _get_db(config):
    from .db import init_db
    return init_db(config)


def extract_messages(jsonl_path: str) -> list[tuple[str, str]]:
    """Extract user and assistant messages from a Claude Code JSONL transcript.

    Returns list of (role, content) tuples.
    """
    messages = []
    try:
        with open(jsonl_path) as f:
            for line in f:
                obj = json.loads(line)
                msg_type = obj.get("type")
                if msg_type not in ("user", "assistant"):
                    continue
                msg = obj.get("message", {})
                content = msg.get("content", "")
                if isinstance(content, list):
                    parts = []
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            parts.append(block["text"])
                        elif isinstance(block, str):
                            parts.append(block)
                    content = "\n".join(parts)
                if not content or not content.strip():
                    continue
                messages.append((msg_type, content.strip()))
    except (OSError, json.JSONDecodeError):
        return []
    return messages


def _haiku_compress(text: str, config: dict) -> str | None:
    """Compress text via direct Haiku API call (curl).

    Bypasses _call_llm which relies on `claude -p` subprocess and fails
    silently when invoked outside an active Claude Code session.
    """
    import subprocess

    key_file = os.path.expanduser(config.get("api_key_file", "~/.engram/api-key"))
    api_key = None
    if os.path.exists(key_file):
        api_key = open(key_file).read().strip()
    if not api_key:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None

    model = config.get("parser", {}).get("haiku_model", "claude-haiku-4-5-20251001")
    system = (
        "Compress this conversation archive into terse notation. "
        "Keep [U] [A] markers. Drop articles/copulas/filler/pronouns. "
        "Use -> (causation), :: (types), | (alternatives), ~ (approx). "
        "Abbreviate common terms: vuln, env, config, auth, fn, conn, impl, app. "
        "Preserve entity names, file paths, commands, error messages, facts, decisions. "
        "Output ONLY the compressed text, no preamble."
    )
    # Budget: allow up to 80% of input length in tokens, capped at 4096
    max_tokens = min(int(len(text) / 4 * 0.8), 4096)
    payload = json.dumps({
        "model": model,
        "max_tokens": max(max_tokens, 256),
        "system": system,
        "messages": [{"role": "user", "content": f"Compress:\n\n{text}"}],
    })
    try:
        result = subprocess.run(
            ["curl", "-s", "--max-time", "30",
             "-H", "Content-Type: application/json",
             "-H", f"x-api-key: {api_key}",
             "-H", "anthropic-version: 2023-06-01",
             "-d", payload,
             "https://api.anthropic.com/v1/messages"],
            capture_output=True, text=True, timeout=32)
        if result.returncode != 0:
            return None
        response = json.loads(result.stdout)
        if response.get("type") == "error":
            return None
        compressed = response.get("content", [{}])[0].get("text", "").strip()
        return compressed or None
    except Exception:
        return None


def _compress_conversation_batch(messages: list[tuple[str, str]], batch_size: int = 3000, config: dict | None = None) -> str:
    """Compress a conversation into dense Strix notation.

    Chunks messages into ~batch_size char groups, compresses each chunk
    via LLM, then joins. Falls back to deterministic if LLM fails.
    """
    if not messages:
        return ""

    # Format the conversation
    formatted_parts = []
    for role, content in messages:
        tag = "U" if role == "user" else "A"
        formatted_parts.append(f"[{tag}] {content}")

    full_text = "\n".join(formatted_parts)

    # For short conversations, compress in one shot
    if len(full_text) < batch_size * 2:
        return _compress_chunk(full_text, config)

    # Chunk by message boundaries (don't split mid-message)
    chunks = []
    current_chunk = []
    current_len = 0
    for part in formatted_parts:
        if current_len + len(part) > batch_size and current_chunk:
            chunks.append("\n".join(current_chunk))
            current_chunk = [part]
            current_len = len(part)
        else:
            current_chunk.append(part)
            current_len += len(part)
    if current_chunk:
        chunks.append("\n".join(current_chunk))

    # Compress each chunk
    compressed_chunks = []
    for chunk in chunks:
        compressed_chunks.append(_compress_chunk(chunk, config))

    return "\n---\n".join(compressed_chunks)


def _compress_chunk(text: str, config: dict | None = None) -> str:
    """Compress a single conversation chunk.

    Applies deterministic compression first, then direct Haiku API for LLM
    compression. Strix _call_llm is not used — it relies on `claude -p`
    subprocess which fails silently outside an active Claude Code session.
    """
    light = compress_deterministic(text) if STRIX_AVAILABLE else text

    if not config:
        return light

    result = _haiku_compress(light, config)
    if result and len(result) < len(light):
        # Apply Strix post-processing if available
        if STRIX_AVAILABLE:
            try:
                result = _sanitize_v11(result)
                result = _post_compress(result)
            except Exception:
                pass
        if len(result) < len(light):
            return result

    return light


def archive_sessions(config=None, dry_run=False, limit=None):
    """Archive all unarchived sessions with Strix compression."""
    if config is None:
        config = _get_config()

    conn = _get_db(config)

    # Find sessions with transcript paths but no archive
    rows = conn.execute("""
        SELECT s.id, s.transcript_path, s.started_at, s.message_count
        FROM sessions s
        LEFT JOIN session_archives sa ON s.id = sa.session_id
        WHERE s.transcript_path IS NOT NULL
          AND sa.session_id IS NULL
        ORDER BY s.started_at DESC
    """).fetchall()

    if limit:
        rows = rows[:limit]

    print(f"  Engram Session Archiver")
    print(f"  =======================")
    print(f"  Unarchived sessions: {len(rows)}")
    print(f"  Mode: {'dry run' if dry_run else 'live'}")

    if not rows:
        print(f"\n  All sessions already archived.")
        conn.close()
        return

    if dry_run:
        total_size = 0
        for sid, path, started, msg_count in rows[:10]:
            size = os.path.getsize(path) if path and os.path.exists(path) else 0
            total_size += size
            print(f"    {started[:10]}  {sid[:30]}...  {msg_count or 0:3d} msgs  {size/1024:.0f}KB")
        if len(rows) > 10:
            print(f"    ... and {len(rows) - 10} more")
        print(f"\n  Would archive {len(rows)} sessions.")
        conn.close()
        return

    archived = 0
    skipped = 0
    total_original = 0
    total_compressed = 0
    t_start = time.time()

    for sid, path, started, msg_count in rows:
        if not path or not os.path.exists(path):
            skipped += 1
            continue

        messages = extract_messages(path)
        if not messages:
            skipped += 1
            continue

        # Format original
        original = "\n".join(f"[{'U' if r=='user' else 'A'}] {c}" for r, c in messages)
        original_chars = len(original)

        # Compress
        compressed = _compress_conversation_batch(messages, config=config)
        compressed_chars = len(compressed)

        # Store
        conn.execute(
            "INSERT OR REPLACE INTO session_archives "
            "(session_id, compressed_text, original_chars, compressed_chars, message_count) "
            "VALUES (?, ?, ?, ?, ?)",
            (sid, compressed, original_chars, compressed_chars, len(messages)))
        conn.commit()

        archived += 1
        total_original += original_chars
        total_compressed += compressed_chars

        if archived % 10 == 0:
            elapsed = time.time() - t_start
            rate = archived / elapsed if elapsed > 0 else 0
            eta = (len(rows) - archived - skipped) / rate if rate > 0 else 0
            ratio = total_original / total_compressed if total_compressed else 0
            print(f"  ... {archived}/{len(rows)} archived ({ratio:.1f}x) [{rate:.1f}/s, ETA {eta/60:.0f}m]")

    elapsed = time.time() - t_start
    ratio = total_original / total_compressed if total_compressed else 0
    print(f"\n  Done: {archived} sessions archived, {skipped} skipped, in {elapsed:.1f}s")
    print(f"  Original: {total_original:,} chars -> Compressed: {total_compressed:,} chars ({ratio:.1f}x)")
    conn.close()


def read_archive(session_id: str, config=None):
    """Read and display a compressed session archive."""
    if config is None:
        config = _get_config()
    conn = _get_db(config)

    row = conn.execute(
        "SELECT compressed_text, original_chars, compressed_chars, message_count, archived_at "
        "FROM session_archives WHERE session_id = ?", (session_id,)).fetchone()

    if not row:
        # Try partial match
        rows = conn.execute(
            "SELECT session_id, compressed_text, original_chars, compressed_chars, message_count, archived_at "
            "FROM session_archives WHERE session_id LIKE ?", (f"%{session_id}%",)).fetchall()
        if not rows:
            print(f"  No archive found for session '{session_id}'")
            conn.close()
            return
        if len(rows) > 1:
            print(f"  Multiple matches:")
            for r in rows:
                print(f"    {r[0]}")
            conn.close()
            return
        row = rows[0][1:]
        session_id = rows[0][0]

    compressed, orig_chars, comp_chars, msg_count, archived_at = row
    ratio = orig_chars / comp_chars if comp_chars else 0

    print(f"  Session: {session_id}")
    print(f"  Archived: {archived_at}")
    print(f"  Messages: {msg_count}")
    print(f"  Compression: {orig_chars:,} -> {comp_chars:,} chars ({ratio:.1f}x)")
    print(f"  {'='*60}")
    print(compressed)
    conn.close()


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Archive conversation transcripts with Strix compression")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be archived")
    parser.add_argument("--limit", type=int, help="Max sessions to archive")
    parser.add_argument("--read", metavar="SESSION_ID", help="Read a compressed archive")
    args = parser.parse_args()

    if args.read:
        read_archive(args.read)
    else:
        archive_sessions(dry_run=args.dry_run, limit=args.limit)


if __name__ == "__main__":
    main()
