# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

Odysseus is a self-hosted AI workspace (chat, agents, research, documents, email, notes, calendar, local model workflows). FastAPI backend + vanilla JS frontend (no framework). Runs via Docker or native Python. Default port 7000.

## Development Setup

```bash
# Docker (recommended)
cp .env.example .env
docker compose up -d --build

# Native
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python setup.py
python -m uvicorn app:app --host 127.0.0.1 --port 7000
```

## Running Checks

```bash
# Full test suite (always use venv Python, not system python3)
./venv/bin/python -m pytest

# Syntax check changed files
python -m py_compile app.py routes/*.py src/*.py
node --check static/js/<file>.js

# Focused tests by taxonomy area
./venv/bin/python tests/run_focus.py --area security
./venv/bin/python tests/run_focus.py --area services --sub-area cookbook
./venv/bin/python tests/run_focus.py --fast          # excludes slow-marked tests
./venv/bin/python tests/run_focus.py --last-failed

# Run a single test file
./venv/bin/python -m pytest tests/test_auth_policy.py -v

# Syntax check before reorganization PRs
python -m compileall src/ routes/ core/
```

## Architecture

### Backend Layer Hierarchy

```
app.py            ← FastAPI entrypoint + router registration
routes/           ← HTTP handlers (54 files, flat) — call into src/
src/              ← Business logic (95 files, flat) — the domain layer
core/             ← Infrastructure: DB models, auth, middleware, session
mcp_servers/      ← MCP server processes (run as subprocesses)
services/         ← Supporting services (search, memory, research, etc.)
integrations/     ← Third-party integration clients (claude, codex)
```

### Key Files

- **`src/constants.py`** — single source of truth for all data paths and config. `core/constants.py` only re-exports from here. Never re-derive paths; import the named constant.
- **`core/database.py`** — 28 SQLAlchemy models, imported by 102 files. Highest-risk file; don't split.
- **`src/agent_loop.py`** — main agentic loop (~2,961 lines). Imported by 22 files.
- **`src/tool_implementations.py`** — 33 `do_*` tool execution functions (~4,032 lines). Imported by 17 files.
- **`src/llm_core.py`** — unified LLM streaming abstraction (Ollama, OpenAI-compatible, Anthropic, etc.).
- **`src/tool_schemas.py`** — JSON Schema definitions for all agent tools.
- **`src/tool_index.py`** — RAG-based tool retrieval from ChromaDB.

### Frontend

No JS framework. All UI is in `static/index.html` + `static/js/*.js` + `static/style.css` (36,653 lines, single file). The JS files are large individual modules (`document.js` ~9,776 lines, `chat.js` ~4,985 lines). No build step; files are served directly.

### Data Persistence

All runtime data lives under `DATA_DIR` (env `ODYSSEUS_DATA_DIR`). Every persisted file has a named constant in `src/constants.py` — use them, don't construct paths with `os.path.join(DATA_DIR, ...)` for named files.

### MCP Servers

`mcp_servers/` contains separate processes: `email_server.py`, `rag_server.py`, `memory_server.py`, `image_gen_server.py`. Managed by `src/mcp_manager.py`.

## Code Conventions

### Paths and URLs

- Import path constants from `src.constants` (e.g. `AUTH_FILE`, `CHROMA_DIR`). Never hardcode `/app/...` or `data/...`.
- For internal API calls, use `internal_api_base()` from `src.constants` — it honors `ODYSSEUS_INTERNAL_BASE` and `APP_PORT`.
- If a data file or directory has no constant yet, add one to `src/constants.py`.

### UI / Frontend

- **No Unicode emoji** in UI or code. Use inline SVG matching the existing monochrome icon style in `static/index.html`.
- Reuse existing CSS variables (`--red`, `--fg`, `--bg`, `--card`, `--border`, etc.). No new color values, font sizes, or spacing units.
- Primary UI font is `Fira Code` (monospaced). Don't override.
- Dark theme is default; light mode goes through the existing theme system only.
- Reuse existing button/input/card/border classes. No parallel widgets.

### Commits

Use [Conventional Commits](https://www.conventionalcommits.org): `type(scope): summary` (e.g. `fix(email): ...`, `feat(notes): ...`). Common types: `fix`, `feat`, `refactor`, `docs`, `test`, `chore`, `ci`. PRs go against `dev`, not `main`.

## Test Taxonomy

Tests are tagged at collection time with `area_*` and `sub_*` markers derived from filenames. Areas: `security`, `routes`, `services`, `cli`, `js`, `helpers`, `unit`, `uncategorized`. CLI tests have moved to `tests/cli/`; most others remain flat under `tests/`. Markers are declared in `pyproject.toml` and registered dynamically by `tests/conftest.py`.

Use `tests/run_focus.py` for focused runs — it validates area/sub-area names and composes filters correctly. Use `./venv/bin/python -m pytest` (not system python3) to avoid missing pinned dependencies.

## Architecture Refactoring Notes

A Phase 0 inventory of the codebase exists at `specs/architecture-runtime-inventory.md`. Key constraints:
- `core/database.py` has 102 importers — highest-risk module, refactor last.
- `src/tool_implementations.py` imports from `routes/` inside function bodies (known cross-layer violations).
- Any file reorganization: add `__init__.py` shims to preserve existing import paths; one domain per PR; no behavior changes mixed with moves.

## GBrain Configuration (configured by /setup-gbrain)
- Mode: remote-http
- MCP URL: https://brain.drunken.io/mcp
- Server version: gbrain v0.36.4.0
- Setup date: 2026-06-21
- MCP registered: yes (user scope)
- Token: stored in ~/.claude.json (do not commit; never written to CLAUDE.md)
- Artifacts repo: https://github.com/nazzzy/gstack-brain.git
- Artifacts sync: full
- Current repo policy: read-write

## GBrain Search Guidance (configured by /sync-gbrain)
<!-- gstack-gbrain-search-guidance:start -->

GBrain is set up and synced on this machine. The agent should prefer gbrain
over Grep when the question is semantic or when you don't know the exact
identifier yet. Two indexed corpora available via the `gbrain` CLI:
- This repo's code (registered as `gstack-code-odysseus` source).
- `~/.gstack/` curated memory (registered as `gstack-brain-sky` source via
  the existing federation pipeline).

Prefer gbrain when:
- "Where is X handled?" / semantic intent, no exact string yet:
    `gbrain search "<terms>"` or `gbrain query "<question>"`
- "Where is symbol Y defined?" / symbol-based code questions:
    `gbrain code-def <symbol>` or `gbrain code-refs <symbol>`
- "What calls Y?" / "What does Y depend on?":
    `gbrain code-callers <symbol>` / `gbrain code-callees <symbol>`
- "What did we decide last time?" / past plans, retros, learnings:
    `gbrain search "<terms>" --source gstack-brain-sky`

Grep is still right for known exact strings, regex, multiline patterns, and
file globs. The brain auto-syncs incrementally on every gstack skill start.
Run `/sync-gbrain` to force-refresh, `/sync-gbrain --full` for full reindex.

<!-- gstack-gbrain-search-guidance:end -->
