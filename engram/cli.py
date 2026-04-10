#!/usr/bin/env python3
"""
Engram — CLI browser for episodic memory.

Usage:
  engram search <query>          Search entities and snippets
  engram entity <name>           Full detail on an entity
  engram sessions                List all sessions
  engram recent [N]              Last N entities (default 20)
  engram stats                   DB statistics
  engram graph <entity>          Entity connection graph
  engram feedback                Feedback history
"""

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path


def _get_config():
    config_path = Path(__file__).resolve().parent / "config.json"
    if not config_path.exists():
        config_path = Path(__file__).resolve().parent / "config.example.json"
    return json.loads(config_path.read_text())


def get_conn():
    config = _get_config()
    db_path = os.path.expanduser(config.get("db_path", "~/.engram/memory.db"))
    if not os.path.exists(db_path):
        print(f"  Database not found at {db_path}")
        print(f"  Run the Engram hook first, or check config.json")
        sys.exit(1)
    conn = sqlite3.connect(db_path, timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def cmd_search(args):
    conn = get_conn()
    query = "%" + args.query.lower() + "%"
    entities = conn.execute(
        "SELECT entity, COUNT(*) as mentions, COUNT(DISTINCT session_id) as sessions "
        "FROM mentions WHERE entity LIKE ? GROUP BY entity ORDER BY mentions DESC", (query,)).fetchall()
    snippets = conn.execute(
        "SELECT entity, session_id, substr(context_snippet,1,120), ts "
        "FROM mentions WHERE context_snippet LIKE ? ORDER BY ts DESC LIMIT 10", (query,)).fetchall()
    if entities:
        print(f"  Entities matching '{args.query}':")
        for e, m, s in entities: print(f"    {e:30s}  {m} mentions, {s} sessions")
    if snippets:
        print(f"\n  Snippets containing '{args.query}':")
        for e, sid, snip, ts in snippets: print(f"    [{ts[:10]}] {e}: {snip}")
    if not entities and not snippets: print(f"  No results for '{args.query}'")
    conn.close()


def cmd_entity(args):
    conn = get_conn()
    entity = args.name.lower()
    mentions = conn.execute("SELECT COUNT(*) FROM mentions WHERE entity=?", (entity,)).fetchone()[0]
    sessions = conn.execute("SELECT COUNT(DISTINCT session_id) FROM mentions WHERE entity=?", (entity,)).fetchone()[0]
    feedback = conn.execute("SELECT COALESCE(SUM(score),0) FROM recall_feedback WHERE entity=?", (entity,)).fetchone()[0]
    if not mentions: print(f"  Entity '{entity}' not found"); conn.close(); return
    print(f"  Entity: {entity}")
    print(f"  Mentions: {mentions} across {sessions} sessions")
    print(f"  Feedback score: {feedback:+d}\n")
    rows = conn.execute(
        "SELECT session_id, MIN(ts), MAX(ts), COUNT(*), "
        "(SELECT context_snippet FROM mentions m2 WHERE m2.session_id=m.session_id AND m2.entity=? ORDER BY ts ASC LIMIT 1) "
        "FROM mentions m WHERE entity=? GROUP BY session_id ORDER BY MIN(ts) DESC", (entity, entity)).fetchall()
    print("  Sessions:")
    for sid, first, last, cnt, snippet in rows:
        print(f"    [{first[:10]}] {sid[:40]}  ({cnt}x)")
        if snippet: print(f"              {snippet[:100]}")
    conn.close()


def cmd_sessions(args):
    conn = get_conn()
    rows = conn.execute(
        "SELECT s.id, s.model, s.message_count, s.started_at, COUNT(m.id) "
        "FROM sessions s LEFT JOIN mentions m ON s.id=m.session_id GROUP BY s.id ORDER BY s.started_at DESC").fetchall()
    print(f"  {len(rows)} sessions:")
    for sid, model, msgs, started, mentions in rows:
        print(f"    {sid[:45]:45s}  {model or '?':6s}  {msgs:3d} msgs  {mentions:3d} entities")
    conn.close()


def cmd_recent(args):
    n = args.n or 20
    conn = get_conn()
    rows = conn.execute(
        "SELECT entity, MAX(ts), COUNT(*), COUNT(DISTINCT session_id) "
        "FROM mentions GROUP BY entity ORDER BY MAX(ts) DESC LIMIT ?", (n,)).fetchall()
    print(f"  Last {len(rows)} entities:")
    for entity, ts, total, sessions in rows:
        print(f"    {ts[:10]}  {entity:30s}  {total}x across {sessions} sessions")
    conn.close()


def cmd_stats(args):
    conn = get_conn()
    config = _get_config()
    db_path = os.path.expanduser(config.get("db_path", "~/.engram/memory.db"))
    sessions = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    mentions = conn.execute("SELECT COUNT(*) FROM mentions").fetchone()[0]
    unique = conn.execute("SELECT COUNT(DISTINCT entity) FROM mentions").fetchone()[0]
    feedback = conn.execute("SELECT COUNT(*) FROM recall_feedback").fetchone()[0]
    surfaced = conn.execute("SELECT COUNT(*) FROM surface_queue WHERE surfaced=1").fetchone()[0]
    size_kb = os.path.getsize(db_path) / 1024 if os.path.exists(db_path) else 0
    top = conn.execute(
        "SELECT entity, COUNT(DISTINCT session_id) FROM mentions GROUP BY entity ORDER BY COUNT(DISTINCT session_id) DESC LIMIT 5").fetchall()
    print(f"  Engram Episodic Memory")
    print(f"  ======================")
    print(f"  Sessions:        {sessions}")
    print(f"  Mentions:        {mentions}")
    print(f"  Unique entities: {unique}")
    print(f"  Feedback entries:{feedback}")
    print(f"  Recalls served:  {surfaced}")
    print(f"  DB size:         {size_kb:.1f} KB")
    if top:
        print(f"\n  Top entities by session spread:")
        for entity, sess in top: print(f"    {entity:30s}  {sess} sessions")
    conn.close()


def cmd_graph(args):
    conn = get_conn()
    entity = args.entity.lower()
    rows = conn.execute(
        "SELECT m2.entity, COUNT(DISTINCT m2.session_id) "
        "FROM mentions m1 JOIN mentions m2 ON m1.session_id=m2.session_id AND m1.entity!=m2.entity "
        "WHERE m1.entity=? GROUP BY m2.entity ORDER BY COUNT(DISTINCT m2.session_id) DESC LIMIT 15", (entity,)).fetchall()
    if not rows: print(f"  No connections for '{entity}'"); conn.close(); return
    print(f"  Connection graph for '{entity}':")
    for related, shared in rows:
        print(f"    {related:30s}  {'#'*shared} ({shared} shared sessions)")
    conn.close()


def _haiku_define(entity: str, snippets: str, config: dict) -> str | None:
    """Use Haiku to generate a 1-2 sentence definition from accumulated snippets."""
    import subprocess
    api_key = None
    key_file = os.path.expanduser(config.get("api_key_file", "~/.engram/api-key"))
    if os.path.exists(key_file):
        api_key = open(key_file).read().strip()
    if not api_key:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    model = config.get("parser", {}).get("haiku_model", "claude-haiku-4-5-20251001")
    prompt = (f"Based on these conversation snippets, write a factual 1-2 sentence definition of what '{entity}' is.\n"
              f"Be specific and concrete. Focus on what it IS, not what was said about it.\n"
              f"Return ONLY the definition text, no preamble.\n\nSnippets:\n{snippets[:1200]}")
    payload = json.dumps({"model": model, "max_tokens": 150,
                          "messages": [{"role": "user", "content": prompt}]})
    try:
        result = subprocess.run(
            ["curl", "-s", "--max-time", "10",
             "-H", "Content-Type: application/json",
             "-H", f"x-api-key: {api_key}",
             "-H", "anthropic-version: 2023-06-01",
             "-d", payload,
             "https://api.anthropic.com/v1/messages"],
            capture_output=True, text=True, timeout=12)
        if result.returncode != 0: return None
        response = json.loads(result.stdout)
        if response.get("type") == "error": return None
        return response.get("content", [{}])[0].get("text", "").strip() or None
    except Exception:
        return None


def cmd_define(args):
    config = _get_config()
    config_path = Path(__file__).resolve().parent / "config.json"
    definitions = config.get("definitions", {})

    if args.auto:
        conn = get_conn()
        min_sessions = args.min_sessions or 3
        rows = conn.execute(
            "SELECT entity, COUNT(DISTINCT session_id) as sessions, "
            "GROUP_CONCAT(context_snippet, ' | ') as snippets "
            "FROM mentions WHERE context_snippet IS NOT NULL "
            "GROUP BY entity HAVING sessions >= ? ORDER BY sessions DESC",
            (min_sessions,)).fetchall()
        conn.close()
        candidates = [(e, s, snip) for e, s, snip in rows if e not in definitions]
        if not candidates:
            print("  All frequent entities already have definitions.")
            return
        print(f"  Generating definitions for {len(candidates)} entities (Haiku)...")
        new_defs = {}
        for entity, sessions, snippets_raw in candidates:
            proposed = _haiku_define(entity, snippets_raw or "", config)
            if proposed:
                print(f"\n  {entity} ({sessions} sessions):")
                print(f"    {proposed}")
                new_defs[entity] = proposed
            else:
                print(f"  {entity}: (skipped — no response)")
        if new_defs and not args.dry_run:
            config["definitions"] = {**definitions, **new_defs}
            config_path.write_text(json.dumps(config, indent=2))
            print(f"\n  Wrote {len(new_defs)} definitions to config.json")
        elif args.dry_run:
            print(f"\n  (dry run — rerun without --dry-run to write)")

    elif args.entity:
        entity = args.entity.lower()
        if args.definition:
            definitions[entity] = args.definition
            config["definitions"] = definitions
            config_path.write_text(json.dumps(config, indent=2))
            print(f"  Defined '{entity}'")
        else:
            defn = definitions.get(entity)
            if defn: print(f"  {entity}: {defn}")
            else: print(f"  No definition for '{entity}'. Use: memory define \"{entity}\" \"text\"")

    else:
        if not definitions:
            print("  No definitions yet. Use 'memory define --auto' or 'memory define <entity> \"text\"'")
            return
        print(f"  {len(definitions)} definitions:")
        for entity, defn in sorted(definitions.items()):
            print(f"    {entity:25s}  {defn[:80]}")


def cmd_densify(args):
    from .densify import densify
    densify(dry_run=args.dry_run, limit=args.limit, light_only=args.light, batch_size=args.batch_size)


def cmd_archive(args):
    from .archive import archive_sessions, read_archive
    if args.read:
        read_archive(args.read)
    else:
        archive_sessions(dry_run=args.dry_run, limit=args.limit)


def cmd_feedback(args):
    conn = get_conn()
    rows = conn.execute(
        "SELECT entity, score, source, user_note, created_at FROM recall_feedback ORDER BY created_at DESC LIMIT 20").fetchall()
    if not rows: print("  No feedback recorded yet"); conn.close(); return
    print("  Recent feedback:")
    for entity, score, source, note, ts in rows:
        print(f"    [{ts[:10]}] {'+' if score>0 else ''}{score} {source:8s}  {entity}")
        if note: print(f"              {note[:80]}")
    conn.close()


def main():
    parser = argparse.ArgumentParser(description="Engram Memory Browser")
    sub = parser.add_subparsers(dest="command")
    p = sub.add_parser("search"); p.add_argument("query")
    p = sub.add_parser("entity"); p.add_argument("name")
    sub.add_parser("sessions")
    p = sub.add_parser("recent"); p.add_argument("n", nargs="?", type=int, default=20)
    sub.add_parser("stats")
    p = sub.add_parser("graph"); p.add_argument("entity")
    sub.add_parser("feedback")
    p = sub.add_parser("define")
    p.add_argument("entity", nargs="?", help="Entity name")
    p.add_argument("definition", nargs="?", help="Definition text to set")
    p.add_argument("--auto", action="store_true", help="Auto-generate definitions using Haiku")
    p.add_argument("--min-sessions", type=int, default=3, dest="min_sessions", help="Min sessions for --auto (default 3)")
    p.add_argument("--dry-run", action="store_true", dest="dry_run", help="Print proposals without writing")
    p = sub.add_parser("densify", help="Compress stored snippets with Strix")
    p.add_argument("--dry-run", action="store_true", dest="dry_run", help="Show what would be compressed")
    p.add_argument("--limit", type=int, help="Max unique snippets to process")
    p.add_argument("--light", action="store_true", help="Deterministic only (no LLM, instant)")
    p.add_argument("--batch-size", type=int, default=20, dest="batch_size", help="Snippets per LLM call")
    p = sub.add_parser("archive", help="Compress conversation transcripts for deep recall")
    p.add_argument("--dry-run", action="store_true", dest="dry_run", help="Show what would be archived")
    p.add_argument("--limit", type=int, help="Max sessions to archive")
    p.add_argument("--read", metavar="SESSION_ID", help="Read a compressed archive")
    args = parser.parse_args()
    if not args.command: parser.print_help(); return
    {"search": cmd_search, "entity": cmd_entity, "sessions": cmd_sessions,
     "recent": cmd_recent, "stats": cmd_stats, "graph": cmd_graph,
     "feedback": cmd_feedback, "define": cmd_define, "densify": cmd_densify,
     "archive": cmd_archive}[args.command](args)


if __name__ == "__main__":
    main()
