"""
Engram — Entity/topic extraction from messages.

Two-stage parser:
  1. Regex — scans for known_entities and aliases from config.json (free, instant)
  2. Haiku — extracts abstract topics regex can't catch (cheap, ~1s latency)

Falls back to regex-only if Haiku API fails or times out.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path


def _load_config() -> dict:
    config_path = Path(__file__).parent / "config.json"
    if not config_path.exists():
        config_path = Path(__file__).parent / "config.example.json"
    return json.loads(config_path.read_text())


def _build_alias_map(config: dict) -> dict[str, str]:
    alias_map = {}
    for canonical, aliases in config.get("aliases", {}).items():
        alias_map[canonical] = canonical
        for alias in aliases:
            alias_map[alias.lower()] = canonical
    return alias_map


def _levenshtein(s1, s2):
    """Levenshtein edit distance (no external deps)."""
    if len(s1) < len(s2): return _levenshtein(s2, s1)
    if len(s2) == 0: return len(s1)
    prev = list(range(len(s2) + 1))
    for i, c1 in enumerate(s1):
        curr = [i + 1]
        for j, c2 in enumerate(s2):
            curr.append(min(prev[j + 1] + 1, curr[j] + 1, prev[j] + (c1 != c2)))
        prev = curr
    return prev[-1]


def _fuzzy_match(entity, config):
    """Match a Haiku-extracted entity to nearest known entity within edit distance.

    Threshold: 1 for 4-5 char entities, 2 for 6+ chars. Under 4 chars: skip.
    """
    if len(entity) < 4:
        return entity
    max_dist = 1 if len(entity) <= 5 else 2

    alias_map = _build_alias_map(config)
    candidates = {}
    for e in config.get("known_entities", []):
        if not e.startswith("_comment"):
            el = e.lower()
            candidates[el] = alias_map.get(el, el)
    for alias, canonical in alias_map.items():
        candidates[alias] = canonical

    best_canonical = None
    best_dist = max_dist + 1
    for candidate, canonical in candidates.items():
        if abs(len(entity) - len(candidate)) > max_dist:
            continue
        d = _levenshtein(entity, candidate)
        if 0 < d < best_dist:
            best_dist = d
            best_canonical = canonical
    return best_canonical if best_canonical else entity


def _regex_extract(text: str, config: dict) -> set[str]:
    text_lower = text.lower()
    found = set()
    for entity in config.get("known_entities", []):
        if entity.startswith("_comment"): continue
        pattern = r'\b' + re.escape(entity.lower()) + r'\b'
        if re.search(pattern, text_lower):
            found.add(entity.lower())
    alias_map = _build_alias_map(config)
    for alias, canonical in alias_map.items():
        pattern = r'\b' + re.escape(alias) + r'\b'
        if re.search(pattern, text_lower):
            found.add(canonical)
    return found


def _haiku_extract(text: str, config: dict) -> set[str]:
    timeout = config.get("parser", {}).get("timeout_seconds", 3)
    model = config.get("parser", {}).get("haiku_model", "claude-haiku-4-5-20251001")
    max_entities = config.get("parser", {}).get("max_entities_per_message", 8)

    api_key = None
    key_file = os.path.expanduser(config.get("api_key_file", "~/.engram/api-key"))
    if os.path.exists(key_file):
        api_key = open(key_file).read().strip()
    if not api_key:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return set()

    prompt = f"""Extract the key topics, entities, and concepts from this message.
Return ONLY a JSON array of lowercase strings. Max {max_entities} items.
Focus on: people, places, projects, abstract concepts being discussed, emotions, decisions.
Do NOT include generic words like "thinking", "question", "thing".

Message: {text[:500]}"""

    payload = json.dumps({
        "model": model, "max_tokens": 150,
        "messages": [{"role": "user", "content": prompt}],
    })

    try:
        result = subprocess.run(
            ["curl", "-s", "--max-time", str(timeout),
             "-H", "Content-Type: application/json",
             "-H", f"x-api-key: {api_key}",
             "-H", "anthropic-version: 2023-06-01",
             "-d", payload,
             "https://api.anthropic.com/v1/messages"],
            capture_output=True, text=True, timeout=timeout + 2)
        if result.returncode != 0: return set()
        response = json.loads(result.stdout)
        if response.get("type") == "error": return set()
        content_text = response.get("content", [{}])[0].get("text", "")
        content_text = content_text.strip().strip("`").strip()
        if content_text.startswith("json"): content_text = content_text[4:].strip()
        entities = json.loads(content_text)
        if isinstance(entities, list):
            return {str(e).lower().strip() for e in entities[:max_entities]}
    except (subprocess.TimeoutExpired, json.JSONDecodeError, KeyError, IndexError):
        pass
    return set()


def extract_entities(text: str, config: dict = None) -> list[str]:
    if config is None: config = _load_config()
    entities = _regex_extract(text, config)
    method = config.get("parser", {}).get("method", "haiku")
    if method == "haiku":
        haiku_entities = _haiku_extract(text, config)
        alias_map = _build_alias_map(config)
        for entity in haiku_entities:
            resolved = alias_map.get(entity)
            if not resolved:
                resolved = _fuzzy_match(entity, config)
            entities.add(resolved)
    negative = set(config.get("negative_entities", []))
    entities = {e for e in entities if e not in negative}
    max_entities = config.get("parser", {}).get("max_entities_per_message", 8)
    return sorted(entities)[:max_entities]


if __name__ == "__main__":
    text = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else sys.stdin.read()
    print(json.dumps(extract_entities(text, _load_config()), indent=2))
