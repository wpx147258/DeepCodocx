"""Fork a session: copy its history into a new, independent session.

The source is never modified and does not need to be unlocked: the copy is
built in a staging directory and renamed into place, so an interrupted fork
cannot leave a half-written session behind.
"""

from __future__ import annotations

import json
import logging
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path

from ..atomic import atomic_write_text
from ..config import AppPaths
from ..errors import SessionError
from .paths import SessionPaths, new_session_id, validate_session_id

logger = logging.getLogger(__name__)


def fork_session(app_paths: AppPaths, source_id: str) -> str:
    """Copy ``source_id`` into a new session and return the new id."""
    validate_session_id(source_id)
    source = SessionPaths.for_session(app_paths, source_id)
    if not source.jsonl.is_file():
        raise SessionError(f"cannot fork unknown session {source_id!r}")

    target_id = new_session_id()
    while app_paths.session_dir(target_id).exists():
        target_id = new_session_id()
    target = SessionPaths.for_session(app_paths, target_id)

    app_paths.sessions_dir.mkdir(parents=True, exist_ok=True)
    staging = app_paths.project_dir / f".fork-{uuid.uuid4().hex[:8]}"
    try:
        shutil.copytree(source.root, staging, ignore=shutil.ignore_patterns("lock"))
        _rewrite_meta(staging / "meta.json", source_id, target_id)
        _rewrite_session_record(staging / "session.jsonl", source_id, target_id)
        staging.rename(target.root)
    except SessionError:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    except OSError as exc:
        shutil.rmtree(staging, ignore_errors=True)
        raise SessionError(f"could not fork session {source_id!r}: {exc}") from exc
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    logger.info("forked session %s into %s", source_id, target_id)
    return target_id


def _rewrite_meta(path: Path, source_id: str, target_id: str) -> None:
    data: dict[str, object] = {}
    if path.is_file():
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            parsed = None
        if isinstance(parsed, dict):
            data = parsed
    data["session_id"] = target_id
    data["forked_from"] = source_id
    data["forked_at"] = datetime.now(UTC).isoformat()
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2))


def _rewrite_session_record(path: Path, source_id: str, target_id: str) -> None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise SessionError(f"could not read {path}: {exc}") from exc
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict) and record.get("type") == "session":
            record["id"] = target_id
            record["forked_from"] = source_id
            lines[index] = json.dumps(record, ensure_ascii=False)
            break
    atomic_write_text(path, "\n".join(lines) + "\n")
