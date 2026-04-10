#!/usr/bin/env python3
"""
Engram Memory Hook — Entry point.

Called by Claude Code on every UserPromptSubmit.
Receives user's message via stdin JSON: {"prompt": "..."}.
Parses entities, writes to DB (trigger fires connections),
outputs recall block to stdout for injection into context.

SAFETY: Hard timeout. If anything fails, outputs nothing
and exits cleanly. Never blocks a session.
"""

import json
import os
import re
import signal
import sys
import traceback
from datetime import date
from pathlib import Path

# Hard timeout — never block the host application
HARD_TIMEOUT = 15  # seconds total (Haiku API can take ~8-10s)

def timeout_handler(signum, frame):
    sys.exit(0)  # silent exit = no output = no injection

signal.signal(signal.SIGALRM, timeout_handler)
signal.alarm(HARD_TIMEOUT)


def get_session_id(data: dict) -> str:
    """Extract session ID from hook input JSON.

    Claude Code passes session_id in the stdin JSON payload.
    Fallback chain: JSON field -> env var -> date-based.
    """
    sid = data.get("session_id")
    if sid:
        return sid

    for var in ("CLAUDE_CODE_SESSION", "CLAUDE_SESSION_ID"):
        val = os.environ.get(var)
        if val:
            return val

    return f"session-{date.today().isoformat()}-{os.getpid()}"


def _find_entity_snippet(text: str, entity: str, max_len: int = None,
                         config: dict = None) -> str:
    """Find the sentence(s) most relevant to an entity."""
    if max_len is None:
        max_len = (config or {}).get("surfacing", {}).get("max_snippet_length", 300)
    sentences = re.split(r'(?<=[.!?\n])\s+', text.strip())
    if len(sentences) <= 1:
        return text[:max_len]

    entity_lower = entity.lower()
    entity_words = entity_lower.split()

    search_terms = {entity_lower}
    if config:
        for canonical, aliases in config.get("aliases", {}).items():
            if canonical == entity_lower:
                search_terms.update(a.lower() for a in aliases)
            for alias in aliases:
                if alias.lower() == entity_lower:
                    search_terms.add(canonical)
                    search_terms.update(a.lower() for a in aliases)

    scored = []
    for s in sentences:
        s_lower = s.lower()
        if entity_lower in s_lower:
            scored.append((3, s))
        elif any(term in s_lower for term in search_terms):
            scored.append((2, s))
        elif len(entity_words) > 1 and any(w in s_lower for w in entity_words):
            scored.append((1, s))

    if not scored:
        return text[:max_len]

    scored.sort(key=lambda x: -x[0])
    best_sentence = scored[0][1]
    best_idx = sentences.index(best_sentence)

    result = best_sentence
    left = best_idx - 1
    right = best_idx + 1
    while len(result) < max_len:
        expanded = False
        if right < len(sentences):
            candidate = result + " " + sentences[right]
            if len(candidate) <= max_len:
                result = candidate
                right += 1
                expanded = True
        if left >= 0:
            candidate = sentences[left] + " " + result
            if len(candidate) <= max_len:
                result = candidate
                left -= 1
                expanded = True
        if not expanded:
            break
    return result


def main():
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return

        data = json.loads(raw)
        prompt = data.get("prompt", "")

        if not prompt or not isinstance(prompt, str):
            return

        if len(prompt.strip()) < 10:
            return

        session_id = get_session_id(data)
        transcript_path = data.get("transcript_path")

        config_path = Path(__file__).parent / "config.json"
        if not config_path.exists():
            config_path = Path(__file__).parent / "config.example.json"
        config = json.loads(config_path.read_text())

        sys.path.insert(0, str(Path(__file__).parent))

        from db import (init_db, ensure_session, write_mention, get_unsurfaced,
                        get_last_surfaced, update_last_surfaced,
                        increment_surfaced_message_index, write_feedback)
        from parser import extract_entities
        from surfacer import format_recall

        conn = init_db(config)
        model = os.environ.get("CLAUDE_MODEL", None)
        ensure_session(conn, session_id, model)

        if transcript_path:
            from db import get_session_transcript_path, set_session_transcript_path
            if not get_session_transcript_path(conn, session_id):
                set_session_transcript_path(conn, session_id, transcript_path)

        entities = extract_entities(prompt, config)

        prev_surfaced = get_last_surfaced(conn, session_id)
        if prev_surfaced:
            for entity, msg_idx in prev_surfaced:
                if msg_idx < 3 and entity in entities:
                    write_feedback(conn, session_id, entity, +1, source="implicit")
            increment_surfaced_message_index(conn, session_id)

        if not entities:
            conn.close()
            return

        # Compress snippets with Strix light mode (deterministic, <1ms, no LLM)
        # Snippets are stored compressed — every recall is already compact.
        try:
            from strix.compress import compress_deterministic as _strix_light
        except ImportError:
            _strix_light = None

        entity_snippets = [
            (entity, _strix_light(snippet) if _strix_light else snippet)
            for entity in entities
            for snippet in [_find_entity_snippet(prompt, entity, config=config)]
        ]

        from collections import defaultdict
        snippet_groups: dict[str, list[str]] = defaultdict(list)
        for entity, snippet in entity_snippets:
            snippet_groups[snippet].append(entity)

        deduped: list[tuple[str, str]] = []
        for snippet, group in snippet_groups.items():
            if len(group) > 2:
                group = sorted(group, key=lambda e: -len(e))[:3]
            for entity in group:
                deduped.append((entity, snippet))

        _comp_level = "light" if _strix_light else "none"
        for entity, snippet in deduped:
            write_mention(
                conn,
                session_id=session_id,
                speaker="user",
                entity=entity,
                raw_text=prompt[:100],
                context_snippet=snippet,
                source="hook",
                compression_level=_comp_level,
            )

        recalls = get_unsurfaced(conn, session_id)
        output = format_recall(recalls, conn=conn, config=config, current_entities=entities)

        if output:
            surfaced_entities = []
            for line in output.split("\n"):
                if line.startswith('- "') and '"' in line[3:]:
                    surfaced_entities.append(line.split('"')[1])
            if surfaced_entities:
                update_last_surfaced(conn, session_id, surfaced_entities)

        # --- Limen: Rule content injection ---
        rule_triggers = config.get("rule_triggers", {})
        if rule_triggers:
            entity_lower_set = {e.lower() for e in entities}
            prompt_lower = prompt.lower()
            rules_dir = os.path.expanduser(
                config.get("rules_dir", "~/.claude/rules")
            )
            triggered_rules = []

            for rule_file, keywords in rule_triggers.items():
                for kw in keywords:
                    kw_lower = kw.lower()
                    matched = (
                        kw_lower in entity_lower_set
                        or kw_lower in prompt_lower
                        or (
                            len(kw_lower.split()) == 1
                            and re.search(
                                r'\b' + re.escape(kw_lower) + r'\b',
                                prompt_lower,
                            )
                        )
                    )
                    if matched:
                        ref_key = f"rule:{rule_file}"
                        already = conn.execute(
                            "SELECT 1 FROM last_surfaced "
                            "WHERE session_id=? AND entity=?",
                            (session_id, ref_key),
                        ).fetchone()
                        if not already:
                            rule_path = os.path.join(rules_dir, rule_file)
                            rule_content = None
                            if os.path.exists(rule_path):
                                try:
                                    raw = open(rule_path).read()
                                    rule_content = (
                                        _strix_light(raw)
                                        if _strix_light
                                        else raw
                                    )
                                except Exception:
                                    pass
                            triggered_rules.append(
                                (rule_file, kw, rule_content)
                            )
                            conn.execute(
                                "INSERT OR REPLACE INTO last_surfaced "
                                "(session_id, entity, surfaced_at, "
                                "message_index) "
                                "VALUES (?, ?, datetime('now'), 0)",
                                (session_id, ref_key),
                            )
                            conn.commit()
                        break
                if len(triggered_rules) >= 2:
                    break

            for rule_file, keyword, content in triggered_rules:
                if content:
                    block = (
                        f"[RULE: {rule_file} — triggered by '{keyword}']\n"
                        f"{content}\n[/RULE]"
                    )
                else:
                    block = (
                        f"[CONTEXT: Read {rules_dir}/{rule_file}"
                        f" — triggered by '{keyword}']"
                    )
                output = f"{output}\n{block}" if output else block

        conn.close()

        if output:
            print(output)

    except SystemExit:
        raise
    except Exception:
        try:
            log_dir = os.path.expanduser("~/.engram")
            os.makedirs(log_dir, exist_ok=True)
            log_path = os.path.join(log_dir, "errors.log")
            with open(log_path, "a") as f:
                f.write(f"\n--- {__import__('datetime').datetime.now().isoformat()} ---\n")
                traceback.print_exc(file=f)
        except Exception:
            pass


if __name__ == "__main__":
    main()
