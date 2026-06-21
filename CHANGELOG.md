# Changelog

## [0.1.0.0] - 2026-06-21

### Added

- Added an External Tasks panel for viewing, filtering, syncing, completing, and resolving conflicts for tasks from external providers.
- Added Todoist task source configuration, encrypted source token storage, background polling, manual sync, and pull/push conflict handling.
- Added the `manage_external_tasks` agent tool so agents can list, create, update, complete, and sync external tasks through the same owner-scoped task model.
- Added contributor-facing repository guidance in `AGENTS.md`.

### Fixed

- Hardened External Tasks source creation to supported presets so custom task sources cannot introduce unsafe fetch targets.
- Made code-navigation grep fall back to the built-in Python search when `rg` returns no hits in temporary workspaces.
