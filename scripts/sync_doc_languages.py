"""Build native, mutually exclusive GitHub language panels from primary content.

Edit the default-open panel in each Chinese/English file, then run this script.
The other panel is generated from the counterpart's primary content.
"""

from __future__ import annotations

import argparse
from pathlib import Path

GROUP = "deepcodocx-doc-language"
LABELS = {"zh-CN": "简体中文", "en": "English"}
GUIDES = ("usage", "configuration", "deepdocx", "architecture", "development", "troubleshooting")
NOTICE = "<!-- Generated language panels. Edit this file's default-open language, then run python scripts/sync_doc_languages.py. -->"


def marker(locale: str, edge: str) -> str:
    return f"<!-- doc-language:{locale}:{edge} -->"


def read_primary(path: Path, locale: str) -> str:
    text = path.read_text(encoding="utf-8")
    start, end = marker(locale, "start"), marker(locale, "end")
    if "<!-- doc-language:" in text:
        if text.count(start) != 1 or text.count(end) != 1:
            raise ValueError(f"{path}: missing or duplicate {locale} content markers")
        content, separator, _ = text.split(start, 1)[1].partition(end)
        if not separator:
            raise ValueError(f"{path}: missing language panel end")
        return content.strip() + "\n"

    # Migrate the previous cross-file language links while keeping Home links.
    lines = text.splitlines()
    for index, line in enumerate(lines[:8]):
        if "简体中文" in line and "English" in line and "|" in line:
            _, _, home = line.partition(" · ")
            lines[index] = home
            break
    return "\n".join(lines).strip() + "\n"


def render(primary_locale: str, content: dict[str, str]) -> str:
    secondary = "en" if primary_locale == "zh-CN" else "zh-CN"
    sections = [NOTICE]
    # The alternative language is collapsed first, so its selector is at the
    # top on initial load. The shared name gives native exclusive-open behavior.
    for locale in (secondary, primary_locale):
        opened = " open" if locale == primary_locale else ""
        sections.append(
            f'<details name="{GROUP}" lang="{locale}"{opened}>\n'
            f"<summary>{LABELS[locale]}</summary>\n\n"
            f"{marker(locale, 'start')}\n"
            f"{content[locale].strip()}\n"
            f"{marker(locale, 'end')}\n\n"
            "</details>"
        )
    return "\n\n".join(sections) + "\n"


def sync_pair(chinese: Path, english: Path, *, check: bool = False) -> list[Path]:
    content = {"zh-CN": read_primary(chinese, "zh-CN"), "en": read_primary(english, "en")}
    changed = []
    for path, locale in ((chinese, "zh-CN"), (english, "en")):
        expected = render(locale, content)
        if path.read_text(encoding="utf-8") != expected:
            changed.append(path)
            if not check:
                path.write_text(expected, encoding="utf-8")
    return changed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true", help="Report stale panels without writing files"
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    pairs = [(root / "README.md", root / "README.en.md")]
    pairs.extend((root / "docs" / f"{name}.md", root / "docs" / f"{name}.en.md") for name in GUIDES)
    changed = [
        path for chinese, english in pairs for path in sync_pair(chinese, english, check=args.check)
    ]
    if args.check and changed:
        print("Stale language panels; run python scripts/sync_doc_languages.py:")
        for path in changed:
            print(path.relative_to(root))
        return 1
    print(
        f"Language panels {'checked' if args.check else 'updated'}: {len(pairs)} bilingual pairs, {len(changed)} changed files"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
