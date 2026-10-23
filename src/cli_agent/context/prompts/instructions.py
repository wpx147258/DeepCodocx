"""Load AGENTS.md and .agents/rules from the workspace."""

from __future__ import annotations

import re
from pathlib import Path

_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_INCLUDE = re.compile(r"^@([^\s]+)$")
_MAX_FILE_CHARS = 200_000


def load_project_instructions(cwd: Path, global_agents_md: Path | None = None) -> str:
    cwd = cwd.resolve()
    files: list[Path] = []
    if global_agents_md is not None and global_agents_md.is_file():
        files.append(global_agents_md)

    ancestors = [cwd, *cwd.parents][::-1]
    for ancestor in ancestors:
        agents = ancestor / "AGENTS.md"
        if agents.is_file():
            files.append(agents)
        rules_dir = ancestor / ".agents" / "rules"
        if rules_dir.is_dir():
            files.extend(sorted(rules_dir.glob("*.md")))
    local = cwd / "AGENTS.local.md"
    if local.is_file():
        files.append(local)

    seen_paths: set[Path] = set()
    chunks: list[str] = []
    for path in files:
        resolved = path.resolve()
        if resolved in seen_paths:
            continue
        seen_paths.add(resolved)
        text = _expand_file(resolved, seen_paths, depth=0)
        if text.strip():
            chunks.append(f"# From {path}\n\n{text.strip()}")
    return "\n\n".join(chunks)


def _expand_file(path: Path, seen: set[Path], depth: int) -> str:
    if depth > 10:
        return "[include depth limit reached]"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return f"[could not read {path}: {exc}]"
    if len(text) > _MAX_FILE_CHARS:
        text = text[:_MAX_FILE_CHARS] + "\n... [file truncated] ..."
    text = _HTML_COMMENT.sub("", text)

    output: list[str] = []
    for line in text.splitlines():
        match = _INCLUDE.match(line.strip())
        if not match:
            output.append(line)
            continue
        raw = match.group(1)
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = path.parent / candidate
        candidate = candidate.resolve()
        if candidate in seen:
            output.append(f"[include cycle: {raw}]")
            continue
        if not candidate.is_file():
            output.append(f"[missing include: {raw}]")
            continue
        seen.add(candidate)
        output.append(_expand_file(candidate, seen, depth + 1))
    return "\n".join(output)
