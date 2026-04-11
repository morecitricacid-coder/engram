"""
Engram — Format recall blocks with decay scoring.

Scores: recency, time-weighted frequency, explicit feedback, implicit feedback.
Ranks by score, caps output, formats into [MEMORY RECALL] block.
"""

import math
import sqlite3
from datetime import datetime

MAX_SESSIONS_PER_ENTITY = 10
MAX_ENTITIES_PER_RECALL = 10


def _cooccurrence_score(conn, entity, current_entities):
    """Bonus for entities that frequently co-occur with currently-mentioned entities."""
    if not current_entities:
        return 0.0
    others = [e for e in current_entities if e != entity]
    if not others:
        return 0.0
    placeholders = ",".join("?" * len(others))
    row = conn.execute(
        f"SELECT COUNT(DISTINCT m1.session_id) "
        f"FROM mentions m1 JOIN mentions m2 ON m1.session_id=m2.session_id "
        f"WHERE m1.entity=? AND m2.entity IN ({placeholders})",
        (entity, *others)
    ).fetchone()
    shared = row[0] if row else 0
    if shared == 0:
        return 0.0
    return min(math.log2(shared + 1) * 0.5, 1.5)


def _score_entity(conn, entity, sessions, current_entities=None):
    """Score entity for recall ranking.

    sessions: list of (date_str, snippet) tuples from parsed recall_text.
    Uses time-weighted frequency (recent sessions count more) and derives
    recency from the same date list to avoid an extra DB query.
    """
    now = datetime.now()
    days_list = []
    for date_str, _ in sessions:
        try:
            days_list.append((now - datetime.fromisoformat(date_str)).total_seconds() / 86400)
        except Exception:
            days_list.append(30.0)

    if not days_list:
        days_ago = 30.0
        decayed_freq = 0.0
    else:
        days_ago = min(days_list)  # most recent session
        # Exponential decay: sessions from 30 days ago contribute exp(-1) ≈ 0.37x
        decayed_freq = sum(math.exp(-d / 30) for d in days_list)

    recency = 1.0 / (days_ago + 1)
    frequency = min(decayed_freq, 3.5)
    fb = conn.execute("SELECT COALESCE(SUM(score),0) FROM recall_feedback WHERE entity=? AND source='explicit'", (entity,)).fetchone()
    explicit = (fb[0] if fb else 0) * 0.5
    imp = conn.execute("SELECT COALESCE(SUM(score),0) FROM recall_feedback WHERE entity=? AND source='implicit'", (entity,)).fetchone()
    implicit = min((imp[0] if imp else 0) * 0.1, 0.3)
    cooccurrence = _cooccurrence_score(conn, entity, current_entities)
    return recency + frequency + explicit + implicit + cooccurrence


def _parse_recall_text(recall_text):
    lines = recall_text.strip().split("\n")
    entity, snippets = "", []
    for line in lines:
        if line.startswith('- "'): entity = line.split('"')[1] if '"' in line else ""
        elif line.strip().startswith("> "):
            content = line.strip()[2:]
            ci = content.find(": ")
            if ci > 0: snippets.append((content[:ci], content[ci+2:]))
    return entity, snippets


def format_recall(recall_texts, conn=None, config=None, current_entities=None):
    max_entities = MAX_ENTITIES_PER_RECALL
    max_sessions = MAX_SESSIONS_PER_ENTITY
    if config:
        max_entities = config.get("surfacing", {}).get("max_recalls_per_message", max_entities)
        max_sessions = config.get("surfacing", {}).get("max_sessions_per_entity", max_sessions)
    if not recall_texts: return ""

    entities = {}
    for text in recall_texts:
        entity, snippets = _parse_recall_text(text)
        if entity and snippets:
            entities.setdefault(entity, []).extend(snippets)
    if not entities: return ""

    for entity in entities:
        seen, unique = set(), []
        for d, s in entities[entity]:
            key = (d, s[:50])
            if key not in seen: seen.add(key); unique.append((d, s))
        entities[entity] = unique

    if conn:
        scored = sorted([(_score_entity(conn, e, s, current_entities), e, s) for e, s in entities.items()], key=lambda x: -x[0])[:max_entities]
    else:
        scored = [(0, e, s) for e, s in entities.items()][:max_entities]

    # P3a: Cross-entity snippet dedup — if two entities share the same snippet
    # (common when extracted from the same message), only show it under the
    # higher-scored entity. Prevents identical text repeated in the recall block.
    global_seen = set()
    s1_links = config.get("s1_links", {}) if config else {}
    definitions = config.get("definitions", {}) if config else {}
    lines = ["[MEMORY RECALL]"]
    for score, entity, snippets in scored:
        unique_snippets = []
        for d, s in snippets[:max_sessions]:
            key = s[:50]
            if key not in global_seen:
                global_seen.add(key)
                unique_snippets.append((d, s))
        if not unique_snippets:
            continue  # All snippets already shown under higher-scored entity
        header = f'- "{entity}" -- {len(unique_snippets)} prior session(s)'
        s1 = s1_links.get(entity)
        if s1: header += f"  [-> {s1}]"
        lines.append(header)
        defn = definitions.get(entity)
        if defn: lines.append(f"  [def: {defn}]")
        for d, s in unique_snippets: lines.append(f"  > {d}: {s}")
    lines.append("[END RECALL]")
    lines.append("(Feedback: /recall)")
    return "\n".join(lines)


def format_prefetch(predictions, conn=None, config=None):
    """Format predictive prefetch block for high-confidence entity predictions.

    predictions: list of (entity, score, evidence_sessions) tuples from
    get_prefetch_predictions(). Only called when score >= threshold.
    """
    if not predictions:
        return ""

    # For each predicted entity, grab the most recent snippet
    lines = []
    for entity, score, evidence in predictions:
        if conn:
            # P3a: Prefer snippet_store, fall back to inline context_snippet
            row = conn.execute(
                "SELECT COALESCE(ss.content, m.context_snippet), m.ts "
                "FROM mentions m LEFT JOIN snippet_store ss ON ss.hash = m.snippet_id "
                "WHERE m.entity=? AND (m.snippet_id IS NOT NULL OR m.context_snippet IS NOT NULL) "
                "ORDER BY m.ts DESC LIMIT 1",
                (entity,)
            ).fetchone()
        else:
            row = None

        snippet = row[0][:120] if row and row[0] else ""
        date_str = row[1][:10] if row and row[1] else ""
        # Score is sum-of-probabilities from multiple seeds, can exceed 1.0.
        # Display as relative strength, not percentage.
        strength = "strong" if score >= 0.5 else "likely" if score >= 0.3 else "possible"

        entry = f'- "{entity}" (predicted: {strength})'
        if snippet and date_str:
            entry += f"\n  > {date_str}: {snippet}"
        lines.append(entry)

    if not lines:
        return ""

    return "[PREFETCH]\n" + "\n".join(lines) + "\n[/PREFETCH]"
