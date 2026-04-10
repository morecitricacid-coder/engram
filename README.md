<div align="center">

<img src="assets/engram-logo.svg" width="100" alt="Engram">

# ENGRAM

*One shared brain across every model.*

[![Python](https://img.shields.io/badge/python-3.10+-a855f7?style=flat-square&logo=python&logoColor=white&labelColor=0d1117)](https://python.org)
[![Version](https://img.shields.io/badge/version-v0.2.0-a855f7?style=flat-square&labelColor=0d1117)](https://github.com/morecitricacid-coder/engram/releases)
[![Dependencies](https://img.shields.io/badge/dependencies-none-22d3ee?style=flat-square&labelColor=0d1117)]()
[![Cost](https://img.shields.io/badge/cost-%7E%243%2Fyear-22d3ee?style=flat-square&labelColor=0d1117)]()
[![License](https://img.shields.io/badge/license-MIT-a855f7?style=flat-square&labelColor=0d1117)](LICENSE)

</div>

---

**Engram** gives your AI coding assistant a persistent memory that works across sessions, across models, and across tools. When you mention a concept in Session 1 with Claude, GPT knows about it in Session 47.

Zero external dependencies. Pure Python + SQLite. ~$3/year for Haiku entity extraction (or $0 with regex-only mode).

---

## The Problem

Every AI coding session starts from zero. Your assistant doesn't remember what you discussed yesterday, what your project names mean, or the decisions you made three sessions ago.

Some tools offer memory — but it's siloed. Claude's memory doesn't help GPT. GPT's memory doesn't help Gemini. When you switch models or tools, you lose everything.

---

## How It Works

Engram runs as a hook on every message. Invisible when it has nothing to say. Surfaces relevant context when it does.

```
You type: "Let's revisit the Fenix pipeline"

Engram fires (invisible, <1s):
  1. Extract entities  →  ["fenix", "pipeline"]
  2. Search SQLite     →  "fenix" appeared in 12 prior sessions
  3. Score + rank      →  recency × frequency × feedback
  4. Inject context:

  [MEMORY RECALL]
  - "fenix" -- 12 prior session(s)
    [def: Real-time data ingestion pipeline with streaming and batch modes]
    > 2026-03-15: built Fenix, a real-time data ingestion pipeline
    > 2026-03-22: Fenix v2 added streaming support, broke the batch endpoint
    > 2026-04-01: fixed Fenix memory leak in the connection pool
  [END RECALL]

Your assistant now has context BEFORE it responds.
```

### Shared Across Models

**Engram's database is shared.** Single SQLite file. Every model reads and writes to it.

```
Session 1  (Claude)  →  "We built Fenix for data ingestion"
Session 12 (GPT)     →  "The Fenix streaming fix landed"
Session 47 (Gemini)  →  sees "fenix" → recall fires from both prior sessions
```

A new model's first session inherits the full entity history from every prior session across every other model. **There is no cold start.**

### Two-Stage Entity Extraction

| Stage | Method | Cost | Catches |
|-------|--------|------|---------|
| Stage 1 | Regex on `known_entities` + `aliases` in config | $0 | Your configured vocabulary |
| Stage 2 | Haiku API (optional) | ~$3/year | Abstract topics regex can't anticipate |
| Stage 3 | Fuzzy match (Levenshtein) on Haiku results | $0 | Typos in LLM extraction |

### Scoring

Not everything surfaces. Engram ranks by relevance:

```
score = recency + frequency + explicit + implicit + cooccurrence

recency       = 1 / (days_ago + 1)
frequency     = log₂(sessions + 1)
explicit      = Σ(score) × 0.5       (/recall good → +1, /recall miss → -1)
implicit      = Σ(score) × 0.1       (capped at 0.3, 3-message window)
cooccurrence  = log₂(shared + 1) × 0.5  (capped at 1.5, associative boost)
```

**Co-occurrence scoring** makes recall associative: if you mention "deployment" and "redis" frequently appears alongside it, "redis" gets a ranking boost even when not mentioned directly. The system learns which concepts travel together.

Max 5 entities per recall. Max 5 sessions per entity. Configurable.

### Fuzzy Entity Matching

LLM entity extraction sometimes introduces typos. Engram auto-corrects using Levenshtein distance against your configured vocabulary:

```
Haiku extracts: "deploymnet"  →  fuzzy match  →  "deployment" (distance 2)
Haiku extracts: "reddis"      →  fuzzy match  →  "redis" (distance 1)
```

Threshold scales by word length: 1 edit for 4-5 char entities, 2 edits for 6+ chars. Under 4 chars: no fuzzy matching (too risky). Zero external dependencies — pure Python implementation.

### Entity Definitions

Give your entities ground-truth definitions so every recall includes not just *when* you discussed something, but *what it is*:

```json
{
  "definitions": {
    "fenix": "Real-time data ingestion pipeline with streaming and batch modes",
    "worker-pool": "Go-based task queue with Redis backing, handles async jobs"
  }
}
```

Definitions appear in recall output as `[def: ...]` lines — the model gets domain context before responding.

Auto-generate definitions from accumulated conversation snippets:

```bash
python3 -m engram.cli define --auto              # Generate for all frequent entities
python3 -m engram.cli define --auto --dry-run     # Preview without writing
python3 -m engram.cli define fenix "My pipeline"  # Set manually
```

---

## Installation

```bash
git clone https://github.com/morecitricacid-coder/engram.git
cd engram

# Configure
cp engram/config.example.json engram/config.json
```

Edit `engram/config.json` — add your project vocabulary:

```json
{
  "known_entities": ["fenix", "worker-pool", "my-tool"],
  "aliases": {
    "fenix": ["the pipeline", "data ingestion"]
  }
}
```

Add the hook to Claude Code (`~/.claude/settings.json`):

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "type": "command",
        "command": "python3 /absolute/path/to/engram/engram/hook.py"
      }
    ]
  }
}
```

The database initializes automatically on first message. That's it.

### Optional: Haiku API for better extraction

For abstract topic extraction (concepts you didn't pre-configure):

```bash
mkdir -p ~/.engram
echo "YOUR-API-KEY" > ~/.engram/api-key
```

---

## CLI

```bash
python3 -m engram.cli stats              # Overview
python3 -m engram.cli search myproject   # Search entities and snippets
python3 -m engram.cli entity myproject   # Full detail on an entity
python3 -m engram.cli recent             # Last 20 entities mentioned
python3 -m engram.cli graph myproject    # What gets mentioned alongside it
python3 -m engram.cli sessions           # All sessions (any model)
python3 -m engram.cli feedback           # Recall feedback history
python3 -m engram.cli define --auto      # Auto-generate entity definitions
python3 -m engram.cli densify            # Compress stored snippets (requires Strix)
python3 -m engram.cli archive            # Archive full conversations (requires Strix)
```

```
$ python3 -m engram.cli stats

  Engram Episodic Memory
  ======================
  Sessions:        47
  Mentions:        1,203
  Unique entities: 89
  Connections:     4,521
  Feedback entries:34
  Recalls served:  156
  DB size:         127.3 KB

  Top entities by session spread:
    fenix                           23 sessions
    deployment                      19 sessions
    auth                            17 sessions
    redis                           15 sessions
    worker-pool                     12 sessions
```

```
$ python3 -m engram.cli graph fenix

  Connection graph for 'fenix':
    pipeline                        #################### (20 shared sessions)
    streaming                       ############ (12 shared sessions)
    redis                           ########## (10 shared sessions)
    deployment                      ######## (8 shared sessions)
    memory leak                     ###### (6 shared sessions)
```

---

## Architecture

```
User types message
       ↓
Claude Code fires UserPromptSubmit hook
       ↓
engram/hook.py receives message via stdin JSON  [15s hard timeout]
       ↓
engram/parser.py extracts entities
  ├── Stage 1: regex on known_entities + aliases
  ├── Stage 2: Haiku API (optional)
  └── Stage 3: fuzzy match typos → canonical (Levenshtein)
       ↓
engram/db.py writes mentions → SQLite WAL
       ↓
SQL trigger fires automatically:
  ├── links new mention to prior sessions
  └── populates surface_queue
       ↓
engram/surfacer.py scores entities (recency + frequency + feedback + co-occurrence)
       ↓
[MEMORY RECALL] printed to stdout → injected into context
```

### Schema

```
sessions ──< mentions ──< connections
                │
                ├──> surface_queue
                ├──> recall_feedback
                └──> last_surfaced
```

| Table | Contents |
|-------|---------|
| `sessions` | One row per conversation (any model) |
| `mentions` | Every entity extraction with context snippet + compression level |
| `connections` | Cross-session entity links (trigger-populated) |
| `surface_queue` | What gets injected (trigger-populated) |
| `recall_feedback` | Explicit + implicit scoring signals |
| `last_surfaced` | 3-message implicit feedback window |

Schema versioning via migrations. Applies automatically on startup. Never breaks existing data.

---

## Safety

| Property | Behavior |
|----------|---------|
| **Hard timeout** | 15 seconds max. Silent exit if anything fails. |
| **Error logging** | Errors go to `~/.engram/errors.log`, never stdout. |
| **Concurrent access** | WAL mode SQLite — safe for parallel reads. |
| **Graceful degradation** | Haiku fails → regex. Regex finds nothing → no output. |

---

## Configuration Reference

```json
{
  "db_path": "~/.engram/memory.db",
  "api_key_file": "~/.engram/api-key",

  "parser": {
    "method": "haiku",
    "fallback": "regex",
    "haiku_model": "claude-haiku-4-5-20251001",
    "timeout_seconds": 10,
    "max_entities_per_message": 8
  },

  "surfacing": {
    "max_recalls_per_message": 5,
    "max_sessions_per_entity": 5,
    "max_snippet_length": 120,
    "enabled": true
  },

  "negative_entities": ["thinking", "question", "thing", "..."],
  "known_entities":    ["fenix", "worker-pool", "my-tool"],
  "aliases":           { "fenix": ["the pipeline"] },
  "definitions":       { "fenix": "Real-time data ingestion pipeline" },
  "s1_links":          { "fenix": "docs/fenix.md" }
}
```

| Field | Purpose |
|-------|---------|
| `db_path` | SQLite database location |
| `api_key_file` | Anthropic API key for Haiku extraction |
| `parser.method` | `"haiku"` (recommended) or `"regex"` (free) |
| `negative_entities` | Words to never extract (too generic) |
| `known_entities` | Your project vocabulary (regex matches these) |
| `aliases` | Alternative names → canonical entity |
| `definitions` | Entity definitions injected into recall as `[def: ...]` |
| `s1_links` | Cross-references to documentation files |

---

## Cost

| Component | Cost | Notes |
|-----------|------|-------|
| Haiku entity extraction | ~$3/year | Optional — regex mode is $0 |
| SQLite storage | ~50KB per 100 sessions | Negligible |
| Recall injection | 50–200 tokens/message | Only fires when relevant |

---

## Relationship to Strix

Engram is the memory layer. [Strix](https://github.com/morecitricacid-coder/strix) is an optional compression layer that sits on top.

When Strix is installed, Engram gains:
- **Light compression** — snippets are stored pre-compressed at write time (deterministic, <1ms, no LLM)
- **`densify` command** — background batch compression of existing snippets
- **`archive` command** — full conversation transcripts compressed for deep recall

Engram works perfectly without Strix. All compression features are optional imports that degrade gracefully.

---

## Why "Engram"?

An engram is the hypothetical physical trace that a memory leaves in the brain — the neural substrate of a stored experience. Engram does the same for AI sessions: every conversation leaves a trace in the database, and those traces surface when relevant context appears again.

---

<div align="center">

[![Strix →](https://img.shields.io/badge/compression_layer-Strix-00d4aa?style=flat-square&labelColor=0d1117)](https://github.com/morecitricacid-coder/strix)

*MIT License*

</div>
