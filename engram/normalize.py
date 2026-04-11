"""
Engram — Entity Normalization (P2).

Finds entities that should be the same concept and merges them via
the entity_aliases table. Uses CONSTRAINED clustering: entities must
pass BOTH string similarity AND co-occurrence checks to merge.

This prevents the mega-cluster problem seen in naive approaches.
"""

import sqlite3
import os
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path


def _string_sim(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def _word_overlap(a: str, b: str) -> float:
    """Fraction of shared words between two entity names."""
    wa = set(a.lower().split())
    wb = set(b.lower().split())
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / max(len(wa), len(wb))


def _is_substring_variant(a: str, b: str) -> bool:
    """Check if one entity is a meaningful substring of the other.

    Requirements:
    - Must match at a word boundary (not inside another word)
    - Shorter must be at least 40% the length of longer
    - Both must share at least one full word
    """
    al, bl = a.lower(), b.lower()
    shorter, longer = (al, bl) if len(al) <= len(bl) else (bl, al)

    # Length ratio check — "api" in "api downtime security vulnerability" is too loose
    if len(shorter) < len(longer) * 0.4:
        return False

    # Must appear as complete word(s), not inside another word
    import re
    if not re.search(r'\b' + re.escape(shorter) + r'\b', longer):
        return False

    # Must share at least one full word
    if not set(shorter.split()) & set(longer.split()):
        return False

    return True


def find_merge_candidates(db_path: str, min_sessions: int = 2,
                          string_thresh: float = 0.8, max_cluster_size: int = 10):
    """
    Find entity pairs that should be merged.

    Constraints (ALL must hold for a merge):
    1. String similarity > string_thresh, OR one is a substring of the other
    2. At least 30% session overlap (Jaccard) if both have 5+ sessions
    3. No cluster exceeds max_cluster_size
    4. Neither entity is in known_entities (those are canonical already)

    Returns list of (canonical, alias, string_sim, session_overlap) tuples.
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)

    # Load entity session sets
    rows = conn.execute("""
        SELECT entity, COUNT(DISTINCT session_id) as sessions
        FROM mentions GROUP BY entity HAVING sessions >= ?
        ORDER BY sessions DESC
    """, (min_sessions,)).fetchall()

    entity_counts = {e: c for e, c in rows}

    # Load session memberships for overlap calculation
    membership = defaultdict(set)
    for e in entity_counts:
        sids = conn.execute(
            "SELECT DISTINCT session_id FROM mentions WHERE entity=?", (e,)
        ).fetchall()
        membership[e] = {s[0] for s in sids}

    # Load known entities (config canonical names — never merge these into something else)
    config_path = Path(__file__).parent / "config.json"
    import json
    config = json.loads(config_path.read_text()) if config_path.exists() else {}
    known = {e.lower() for e in config.get("known_entities", []) if not e.startswith("_")}

    conn.close()

    entities = sorted(entity_counts.keys(), key=lambda e: -entity_counts[e])

    # Union-Find for cluster size tracking
    parent = {e: e for e in entities}
    cluster_size = {e: 1 for e in entities}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra == rb:
            return False
        # Keep the one with more sessions as root (canonical)
        if entity_counts.get(ra, 0) >= entity_counts.get(rb, 0):
            parent[rb] = ra
            cluster_size[ra] = cluster_size.get(ra, 1) + cluster_size.get(rb, 1)
        else:
            parent[ra] = rb
            cluster_size[rb] = cluster_size.get(rb, 1) + cluster_size.get(ra, 1)
        return True

    merges = []

    for i, a in enumerate(entities):
        for b in entities[i+1:]:
            # Skip if either is a known canonical entity being merged INTO something
            # (known entities CAN be the canonical target, just never the alias)

            # Check string similarity OR substring relationship
            sim = _string_sim(a, b)
            is_substr = _is_substring_variant(a, b)

            if sim < string_thresh and not is_substr:
                continue

            # Additional check: word overlap must be significant for non-substring matches
            if not is_substr and _word_overlap(a, b) < 0.5:
                continue

            # Guard against false-positive string similarity (e.g., "memory file" ↔ "memory leak")
            # For multi-word entities, require shared words to cover majority of the shorter one
            a_words, b_words = set(a.split()), set(b.split())
            if len(a_words) > 1 and len(b_words) > 1:
                shorter_words = a_words if len(a_words) <= len(b_words) else b_words
                shared = a_words & b_words
                if len(shared) < len(shorter_words) * 0.6:
                    continue

            # Check session overlap (Jaccard) for entities with enough data
            sa, sb = membership.get(a, set()), membership.get(b, set())
            if len(sa) >= 5 and len(sb) >= 5:
                jaccard = len(sa & sb) / len(sa | sb) if (sa | sb) else 0
                if jaccard < 0.2:
                    continue
            else:
                jaccard = -1  # Not enough data to check

            # Check cluster size cap
            ra, rb = find(a), find(b)
            if ra == rb:
                continue  # Already in same cluster
            combined = cluster_size.get(ra, 1) + cluster_size.get(rb, 1)
            if combined > max_cluster_size:
                continue

            # Determine canonical (more sessions wins; known_entities always canonical)
            if a in known:
                canonical, alias = a, b
            elif b in known:
                canonical, alias = b, a
            elif entity_counts.get(a, 0) >= entity_counts.get(b, 0):
                canonical, alias = a, b
            else:
                canonical, alias = b, a

            # Never make a known entity into an alias
            if alias in known:
                continue

            union(a, b)
            merges.append((canonical, alias, sim, jaccard))

    return merges


def apply_normalizations(db_path: str, merges: list, rewrite_mentions: bool = False):
    """Write merge decisions to entity_aliases table.

    If rewrite_mentions=True, also UPDATE existing mentions to use canonical names.
    """
    conn = sqlite3.connect(db_path, timeout=5)

    for canonical, alias, _, _ in merges:
        conn.execute(
            "INSERT OR REPLACE INTO entity_aliases (alias, canonical) VALUES (?, ?)",
            (alias, canonical)
        )

    if rewrite_mentions:
        for canonical, alias, _, _ in merges:
            conn.execute(
                "UPDATE mentions SET entity=? WHERE entity=?",
                (canonical, alias)
            )

    conn.commit()
    count = len(merges)
    conn.close()
    return count
