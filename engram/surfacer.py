"""
Engram — Format recall blocks with decay scoring.

Scores: recency, frequency, explicit feedback, implicit feedback.
Ranks by score, caps output, formats into [MEMORY RECALL] block.
"""

import math
import sqlite3
from datetime import datetime

MAX_SESSIONS_PER_ENTITY = 5
MAX_ENTITIES_PER_RECALL = 5


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


def _score_entity(conn, entity, session_count, current_entities=None):
    row = conn.execute("SELECT MAX(ts) FROM mentions WHERE entity = ?", (entity,)).fetchone()
    if row and row[0]:
        try: days_ago = (datetime.now() - datetime.fromisoformat(row[0])).total_seconds() / 86400
        except: days_ago = 30
    else: days_ago = 30
    recency = 1.0 / (days_ago + 1)
    frequency = math.log2(session_count + 1)
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
        scored = sorted([((_score_entity(conn, e, len(s), current_entities), e, s)) for e, s in entities.items()], key=lambda x: -x[0])[:max_entities]
    else:
        scored = [(0, e, s) for e, s in entities.items()][:max_entities]

    s1_links = config.get("s1_links", {}) if config else {}
    definitions = config.get("definitions", {}) if config else {}
    lines = ["[MEMORY RECALL]"]
    for score, entity, snippets in scored:
        snippets = snippets[:max_sessions]
        header = f'- "{entity}" -- {len(snippets)} prior session(s)'
        s1 = s1_links.get(entity)
        if s1: header += f"  [-> {s1}]"
        lines.append(header)
        defn = definitions.get(entity)
        if defn: lines.append(f"  [def: {defn}]")
        for d, s in snippets: lines.append(f"  > {d}: {s}")
    lines.append("[END RECALL]")
    return "\n".join(lines)
