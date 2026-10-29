from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path

import pytest
from scripts.sync_doc_languages import GROUP, marker, read_primary, render, sync_pair


class Panels(HTMLParser):
    def __init__(self):
        super().__init__()
        self.details = []

    def handle_starttag(self, tag, attrs):
        if tag == "details":
            self.details.append(dict(attrs))


@pytest.mark.parametrize("locale", ["zh-CN", "en"])
def test_language_selectors_are_native_exclusive_panels(locale):
    text = render(locale, {"zh-CN": "# 中文\n\n正文", "en": "# English\n\nBody"})
    parser = Panels()
    parser.feed(text)
    assert len(parser.details) == 2
    assert all(panel["name"] == GROUP for panel in parser.details)
    assert [panel["lang"] for panel in parser.details if "open" in panel] == [locale]
    assert "open" not in parser.details[0]
    assert "<summary>English</summary>" in text and "<summary>简体中文</summary>" in text
    assert "<a href=" not in text


def test_conversion_preserves_home_and_primary_markdown(tmp_path: Path):
    chinese = tmp_path / "guide.md"
    english = tmp_path / "guide.en.md"
    chinese.write_text(
        "# 中文\n\n**简体中文** | [English](guide.en.md) · [首页](../README.md)\n\n原文\n"
    )
    english.write_text(
        "# English\n\n[简体中文](guide.md) | **English** · [Home](../README.en.md)\n\nOriginal\n"
    )
    assert sync_pair(chinese, english) == [chinese, english]
    assert read_primary(chinese, "zh-CN") == "# 中文\n\n[首页](../README.md)\n\n原文\n"
    assert read_primary(english, "en") == "# English\n\n[Home](../README.en.md)\n\nOriginal\n"
    assert sync_pair(chinese, english, check=True) == []
    assert sync_pair(chinese, english) == []


def test_primary_edit_updates_counterpart_without_recursive_copies(tmp_path: Path):
    chinese = tmp_path / "README.md"
    english = tmp_path / "README.en.md"
    chinese.write_text("# 中文\n\n原文\n")
    english.write_text("# English\n\nOriginal\n")
    sync_pair(chinese, english)
    chinese.write_text(chinese.read_text().replace("原文", "更新后的原文"))
    before = {path: path.read_bytes() for path in (chinese, english)}
    assert sync_pair(chinese, english, check=True) == [english]
    assert {path: path.read_bytes() for path in before} == before
    assert sync_pair(chinese, english) == [english]
    assert read_primary(english, "zh-CN").endswith("更新后的原文\n")
    assert english.read_text().count(marker("en", "start")) == 1
    assert english.read_text().count(marker("zh-CN", "start")) == 1


def test_incomplete_markers_are_not_silently_overwritten(tmp_path: Path):
    path = tmp_path / "README.md"
    path.write_text(marker("zh-CN", "start") + "\nUnfinished content")
    with pytest.raises(ValueError, match="markers"):
        read_primary(path, "zh-CN")
