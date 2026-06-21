"""Regression coverage for the External Tasks side-panel renderer."""

from pathlib import Path


SRC = Path(__file__).resolve().parent.parent / "static/js/externalTasks.js"


def test_external_tasks_panel_uses_element_scoped_selectors():
    """Regression: ISSUE-001 — panel stayed on Loading after API success.

    Found by /qa on 2026-06-21.
    Report: .gstack/qa-reports/qa-report-localhost-2026-06-21.md
    """
    text = SRC.read_text(encoding="utf-8")

    assert "(pane || document).getElementById" not in text
    assert "(pane || document).querySelector('#ext-tasks-filters')" in text
    assert "(pane || document).querySelector('#ext-tasks-list')" in text
    assert "(pane || document).querySelector('#ext-tasks-sources-drawer')" in text
