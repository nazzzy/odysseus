# Repository Guidelines

## Project Structure & Module Organization

Odysseus is a self-hosted AI workspace. The main app entrypoint is `app.py`, with HTTP/API handlers in `routes/`, domain services in `services/`, shared implementation in `src/`, and compatibility exports in `core/`. Static UI assets live in `static/` (`static/js/`, `static/icons/`, `static/fonts/`). Documentation is in `docs/`; packaging, deployment, and utility scripts are in `scripts/`, `docker/`, and the root shell/PowerShell launchers. Tests live in `tests/`, with helper code under `tests/helpers/`.

## Build, Test, and Development Commands

- `cp .env.example .env && docker compose up -d --build`: recommended local startup path; app listens on `http://localhost:7000`.
- `python3 -m venv venv && source venv/bin/activate && pip install -r requirements.txt`: native Python setup.
- `python -m uvicorn app:app --host 127.0.0.1 --port 7000`: run the app without Docker.
- `python -m pytest`: run the full Python test suite.
- `python -m py_compile app.py routes/*.py src/*.py`: quick syntax check for core Python files.
- `node --check static/js/<file>.js`: syntax check changed frontend JavaScript.

## Coding Style & Naming Conventions

Use Python 3.11+ conventions: 4-space indentation, `snake_case` for functions/modules, `PascalCase` for classes, and focused modules with explicit imports. Do not hardcode writable paths, loopback URLs, ports, or shared limits. Prefer constants from `src/constants.py`; `core/constants.py` exists for backward-compatible re-exports. UI changes should reuse existing CSS variables, classes, dark-theme styling, and monochrome icon language. Do not add emoji to UI or code.

## Testing Guidelines

Tests are pytest-based and configured in `pyproject.toml`. Name tests `test_*.py` and keep them near the behavior they cover. Use focused taxonomy runs when possible:

```bash
./venv/bin/python tests/run_focus.py --area security
./venv/bin/python tests/run_focus.py --area services --sub-area cookbook
./venv/bin/python tests/run_focus.py --fast
```

Mark tests `slow` only with duration evidence. Do not silence failures with broad `skip`/`xfail`; fix isolation or behavior issues directly.

## Commit & Pull Request Guidelines

Commits follow Conventional Commits: `type(scope): summary`, for example `fix(search): validate provider URL` or `docs(contributing): clarify Docker setup`. Open PRs against `dev`, keep them small, and avoid mixing refactors with behavior changes. PR descriptions should explain the change, list test/manual verification commands, link related issues (`Fixes #123`), and include screenshots or short recordings for UI changes.

## Security & Configuration Tips

Never commit secrets, API keys, private logs, personal documents, or generated local data. Keep auth enabled for exposed deployments, avoid publishing raw model/service ports, and follow `SECURITY.md` for vulnerability reports.
