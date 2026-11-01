"""Static structure checks for the browser shell.

The sidebar keeps a single document-library entry; library data, ingestion and
index management live in one settings-style dialog. Model selection belongs to
the top bar in both modes.
"""

from __future__ import annotations

from dcx.web.server import STATIC_DIR


def _html() -> str:
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


def _between(text: str, start: str, end: str) -> str:
    begin = text.index(start)
    return text[begin : text.index(end, begin)]


def test_sidebar_has_only_the_library_manager_entry() -> None:
    sidebar = _between(_html(), '<aside class="sidebar"', "</aside>")

    assert 'id="library-open"' in sidebar
    assert "文档库管理" in sidebar
    for removed in (
        'id="document-search"',
        'id="document-list"',
        'id="ingest-open"',
        'id="index-open"',
        'id="semantic-status"',
    ):
        assert removed not in sidebar


def test_model_selection_is_in_the_top_bar() -> None:
    html = _html()
    sidebar = _between(html, '<aside class="sidebar"', "</aside>")
    topbar = _between(html, '<header class="topbar"', "</header>")

    assert 'id="model-select"' in topbar
    assert 'id="model-select"' not in sidebar


def test_library_dialog_contains_the_three_settings_sections() -> None:
    html = _html()
    dialog = _between(html, '<dialog id="library-dialog"', "</dialog>")

    for panel in ("overview", "add", "index"):
        assert f'id="library-panel-{panel}"' in dialog
    assert 'id="document-search"' in dialog
    assert 'id="document-list"' in dialog
    assert 'id="ingest-form"' in dialog
    assert 'id="index-sync"' in dialog

    assert 'id="ingest-dialog"' not in html
    assert 'id="index-dialog"' not in html


def test_math_assets_are_vendored_and_wired() -> None:
    html = _html()
    for asset in (
        "vendor/katex/katex.min.css",
        "vendor/katex/katex.min.js",
        "vendor/katex/auto-render.min.js",
    ):
        assert asset in html
        assert (STATIC_DIR / asset).is_file()
    assert (STATIC_DIR / "vendor/katex/fonts").is_dir()
