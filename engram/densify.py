#!/usr/bin/env python3
"""
Engram densifier — background compression of stored snippets.

Finds mentions with compression_level != 'dense', compresses their
context_snippet using Strix, and updates the DB. Processes unique
snippets in batches to minimize LLM calls.

Usage:
  python3 -m engram.densify              # Densify all un-compressed snippets
  python3 -m engram.densify --dry-run    # Show what would be compressed
  python3 -m engram.densify --limit 100  # Process at most 100 unique snippets
  python3 -m engram.densify --light      # Deterministic only (no LLM, instant)
"""

import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

# Strix compression
try:
    from strix.compress import compress_deterministic, _compress_llm, _sanitize_v11, _post_compress, _call_llm
    STRIX_AVAILABLE = True
except ImportError:
    STRIX_AVAILABLE = False


def _get_config():
    config_path = Path(__file__).parent / "config.json"
    if not config_path.exists():
        config_path = Path(__file__).parent / "config.example.json"
    return json.loads(config_path.read_text())


def _get_db(config):
    from .db import get_db_path, init_db
    return init_db(config)


def _batch_compress_llm(snippets: list[str], timeout: int = 120) -> list[str]:
    """Compress multiple snippets in a single LLM call.

    Sends numbered snippets, expects numbered compressed results.
    Falls back to individual compression if batch parsing fails.
    """
    if not snippets:
        return []

    numbered = "\n".join(f"[{i+1}] {s}" for i, s in enumerate(snippets))

    system = """You compress text snippets into terse notation.

RULES:
- Drop: articles (a/an/the), copulas (is/are/was/were), filler, pronouns
- Use: -> for causation, :: for types, | for alternatives, ~ for approx
- Use abbreviations: vuln, env, config, auth, fn, conn, inst
- "$X per month" -> "$X/mo"
- Keep entity names intact and searchable
- Keep ALL facts — remove only grammatical filler
- Output ONLY the compressed snippets, numbered to match input"""

    prompt = f"Compress each snippet. Preserve numbering.\n\n{numbered}"

    try:
        result = _call_llm(prompt, system=system, timeout=timeout)
    except Exception:
        return [compress_deterministic(s) for s in snippets]

    result = _sanitize_v11(result)

    # Parse numbered results
    lines = result.strip().split("\n")
    compressed = {}
    current_num = None
    current_text = []

    for line in lines:
        m = re.match(r'^\[(\d+)\]\s*(.*)', line)
        if m:
            if current_num is not None and current_text:
                compressed[current_num] = _post_compress(" ".join(current_text))
            current_num = int(m.group(1))
            current_text = [m.group(2)] if m.group(2) else []
        elif current_num is not None:
            current_text.append(line.strip())

    if current_num is not None and current_text:
        compressed[current_num] = _post_compress(" ".join(current_text))

    # Build result list, falling back to deterministic for any missing
    results = []
    for i, snippet in enumerate(snippets):
        if (i + 1) in compressed and compressed[i + 1].strip():
            results.append(compressed[i + 1])
        else:
            results.append(compress_deterministic(snippet))

    return results


def densify(config=None, dry_run=False, limit=None, light_only=False, batch_size=20):
    """Compress all un-densified snippets in the DB.

    Args:
        config: Engram config dict. Auto-loaded if None.
        dry_run: If True, show what would be done without changing anything.
        limit: Max number of unique snippets to process. None = all.
        light_only: If True, use deterministic compression only (no LLM).
        batch_size: Number of snippets per LLM call.
    """
    if not STRIX_AVAILABLE:
        print("ERROR: strix-memory not installed. Run: pip install -e ~/strix")
        return

    if config is None:
        config = _get_config()

    conn = _get_db(config)
    target_level = "light" if light_only else "dense"

    # Get unique snippets that need compression, with their mention IDs
    if target_level == "dense":
        where = "compression_level != 'dense'"
    else:
        where = "compression_level = 'none'"

    rows = conn.execute(f"""
        SELECT context_snippet, GROUP_CONCAT(id) as mention_ids, COUNT(*) as cnt
        FROM mentions
        WHERE context_snippet IS NOT NULL AND {where}
        GROUP BY context_snippet
        ORDER BY cnt DESC
    """).fetchall()

    if limit:
        rows = rows[:limit]

    total_unique = len(rows)
    total_mentions = sum(r[2] for r in rows)

    print(f"  Engram Densifier")
    print(f"  ================")
    print(f"  Unique snippets:  {total_unique}")
    print(f"  Total mentions:   {total_mentions}")
    print(f"  Target level:     {target_level}")
    print(f"  Mode:             {'dry run' if dry_run else 'live'}")

    if dry_run:
        print(f"\n  Sample (first 5):")
        for snippet, ids, cnt in rows[:5]:
            compressed = compress_deterministic(snippet)
            print(f"    [{cnt}x] {snippet[:80]}...")
            print(f"     -> {compressed[:80]}...")
        print(f"\n  Would process {total_unique} unique snippets ({total_mentions} mentions).")
        conn.close()
        return

    if total_unique == 0:
        print(f"\n  Nothing to densify — all snippets already at '{target_level}'.")
        conn.close()
        return

    # Process
    processed = 0
    mentions_updated = 0
    t_start = time.time()

    if light_only:
        # Deterministic — instant, no batching needed
        for snippet, ids_str, cnt in rows:
            compressed = compress_deterministic(snippet)
            id_list = [int(x) for x in ids_str.split(",")]
            placeholders = ",".join("?" * len(id_list))
            conn.execute(
                f"UPDATE mentions SET context_snippet = ?, compression_level = 'light' WHERE id IN ({placeholders})",
                [compressed] + id_list)
            processed += 1
            mentions_updated += cnt
            if processed % 500 == 0:
                conn.commit()
                print(f"  ... {processed}/{total_unique} unique ({mentions_updated} mentions)")

        conn.commit()
    else:
        # LLM batched compression
        batch = []
        batch_meta = []  # (ids_str, cnt) per batch item

        for snippet, ids_str, cnt in rows:
            # First apply deterministic pre-compression
            light = compress_deterministic(snippet)
            batch.append(light)
            batch_meta.append((ids_str, cnt))

            if len(batch) >= batch_size:
                _process_batch(conn, batch, batch_meta)
                processed += len(batch)
                mentions_updated += sum(m[1] for m in batch_meta)
                elapsed = time.time() - t_start
                rate = processed / elapsed if elapsed > 0 else 0
                eta = (total_unique - processed) / rate if rate > 0 else 0
                print(f"  ... {processed}/{total_unique} unique ({mentions_updated} mentions) "
                      f"[{rate:.1f}/s, ETA {eta/60:.0f}m]")
                batch = []
                batch_meta = []

        # Final batch
        if batch:
            _process_batch(conn, batch, batch_meta)
            processed += len(batch)
            mentions_updated += sum(m[1] for m in batch_meta)

    elapsed = time.time() - t_start
    print(f"\n  Done: {processed} unique snippets ({mentions_updated} mentions) in {elapsed:.1f}s")
    conn.close()


def _process_batch(conn, snippets, meta):
    """Compress a batch of snippets via LLM and update the DB."""
    compressed_list = _batch_compress_llm(snippets)

    for compressed, (ids_str, cnt) in zip(compressed_list, meta):
        id_list = [int(x) for x in ids_str.split(",")]
        placeholders = ",".join("?" * len(id_list))
        conn.execute(
            f"UPDATE mentions SET context_snippet = ?, compression_level = 'dense' WHERE id IN ({placeholders})",
            [compressed] + id_list)

    conn.commit()


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Densify Engram memory snippets with Strix compression")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be compressed")
    parser.add_argument("--limit", type=int, help="Max unique snippets to process")
    parser.add_argument("--light", action="store_true", help="Deterministic only (no LLM, instant)")
    parser.add_argument("--batch-size", type=int, default=20, help="Snippets per LLM call (default 20)")
    args = parser.parse_args()

    densify(
        dry_run=args.dry_run,
        limit=args.limit,
        light_only=args.light,
        batch_size=args.batch_size,
    )


if __name__ == "__main__":
    main()
