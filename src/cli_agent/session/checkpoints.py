"""Per-turn file checkpoints for rewinding code alongside the transcript.

File-editing tools snapshot a file before their first write in a user turn.
The manifest keeps the pre-image (content-addressed under the session) and the
post-write hash, so a rewind can restore the workspace and detect files that
were changed manually since then. Shell side effects are recorded as a warning
only; they are not generally reversible.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import re
import stat as stat_module
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..atomic import atomic_write_bytes, atomic_write_text
from .paths import SessionPaths

logger = logging.getLogger(__name__)

MAX_CHECKPOINTS = 100
MAX_AGE_DAYS = 30
MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_TURN_BYTES = 50 * 1024 * 1024
_SHELL_COMMAND_CHARS = 200
_TURN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


@dataclass(slots=True)
class _Entry:
    before_state: str
    before_hash: str | None
    before_mode: int | None
    after_state: str
    after_hash: str | None
    after_mode: int | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "before": {
                "state": self.before_state,
                "hash": self.before_hash,
                "mode": self.before_mode,
            },
            "after": {
                "state": self.after_state,
                "hash": self.after_hash,
                "mode": self.after_mode,
            },
        }


@dataclass(frozen=True, slots=True)
class PreviewFile:
    path: str
    status: str
    conflict: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "status": self.status,
            "conflict": self.conflict,
        }


@dataclass(frozen=True, slots=True)
class CheckpointPreview:
    files: tuple[PreviewFile, ...] = ()
    shell_ran: bool = False
    commands: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    partial: bool = False
    restorable: bool = True

    @property
    def has_changes(self) -> bool:
        return bool(self.files)

    @property
    def conflicts(self) -> tuple[str, ...]:
        return tuple(item.path for item in self.files if item.conflict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "files": [item.to_dict() for item in self.files],
            "has_changes": self.has_changes,
            "shell_ran": self.shell_ran,
            "commands": list(self.commands),
            "warnings": list(self.warnings),
            "partial": self.partial,
            "restorable": self.restorable,
            "conflicts": list(self.conflicts),
        }


@dataclass(frozen=True, slots=True)
class RestoreResult:
    restored: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    aborted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "restored": list(self.restored),
            "skipped": list(self.skipped),
            "conflicts": list(self.conflicts),
            "warnings": list(self.warnings),
            "aborted": self.aborted,
        }


@dataclass(slots=True)
class _Manifest:
    turn_id: str
    created_at: str
    workspace: str
    files: dict[str, _Entry] = field(default_factory=dict)
    shell_ran: bool = False
    commands: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    partial: bool = False


class CheckpointStore:
    def __init__(
        self,
        paths: SessionPaths,
        workspace: Path,
        *,
        max_checkpoints: int = MAX_CHECKPOINTS,
        max_age_days: int = MAX_AGE_DAYS,
        max_file_bytes: int = MAX_FILE_BYTES,
        max_turn_bytes: int = MAX_TURN_BYTES,
    ) -> None:
        self.paths = paths
        self.workspace = workspace.resolve()
        self.max_checkpoints = max_checkpoints
        self.max_age_days = max_age_days
        self.max_file_bytes = max_file_bytes
        self.max_turn_bytes = max_turn_bytes
        self._turns_dir = paths.checkpoints / "turns"
        self._blobs_dir = paths.checkpoints / "blobs"
        self._turn_id: str | None = None
        self._files: dict[str, _Entry] = {}
        self._commands: list[str] = []
        self._warnings: list[str] = []
        self._partial = False
        self._bytes = 0
        self._outside_warned: set[str] = set()

    # ------------------------------------------------------------------ capture

    def begin(self, turn_id: str) -> None:
        if not _TURN_ID.fullmatch(turn_id) or ".." in turn_id:
            logger.warning("ignoring invalid checkpoint turn id: %r", turn_id)
            self._turn_id = None
            return
        self._turn_id = turn_id
        self._files = {}
        self._commands = []
        self._warnings = []
        self._partial = False
        self._bytes = 0
        self._outside_warned = set()
        try:
            self._manifest_path(turn_id).unlink(missing_ok=True)
        except OSError:
            logger.debug("could not replace old checkpoint manifest", exc_info=True)
        self._prune()

    def capture(self, raw_path: str) -> None:
        """Snapshot a path before a write tool changes it."""
        if self._turn_id is None:
            return
        resolved = self._relative_path(raw_path)
        if resolved is None:
            self._write()
            return
        rel, path = resolved
        if rel in self._files:
            return
        try:
            info = path.stat()
        except FileNotFoundError:
            info = None
        except OSError:
            self._warn(f"{rel}: cannot stat; not checkpointed")
            self._write()
            return
        if info is not None and info.st_size > self.max_file_bytes:
            self._partial = True
            self._warn(f"{rel}: larger than the 5 MiB checkpoint limit")
            self._write()
            return
        if info is not None and self._bytes + info.st_size > self.max_turn_bytes:
            self._partial = True
            self._warn("turn checkpoint exceeded its 50 MiB limit")
            self._write()
            return
        state = self._read_state(path)
        if state is None:
            self._warn(f"{rel}: not a regular file; not checkpointed")
            self._write()
            return
        kind, digest, mode, data = state
        if data is not None and len(data) > self.max_file_bytes:
            self._partial = True
            self._warn(f"{rel}: larger than the 5 MiB checkpoint limit")
            return
        size = len(data) if data is not None else 0
        if size and self._bytes + size > self.max_turn_bytes:
            self._partial = True
            self._warn("turn checkpoint exceeded its 50 MiB limit")
            return
        if digest is not None and data is not None:
            self._store_blob(digest, data)
        self._bytes += size
        self._files[rel] = _Entry(
            before_state=kind,
            before_hash=digest,
            before_mode=mode,
            after_state=kind,
            after_hash=digest,
            after_mode=mode,
        )
        self._write()

    def record_after(self, raw_path: str) -> None:
        """Record the state after a write tool ran."""
        if self._turn_id is None:
            return
        resolved = self._relative_path(raw_path)
        if resolved is None:
            return
        rel, path = resolved
        entry = self._files.get(rel)
        if entry is None:
            return
        state = self._read_state(path)
        if state is None:
            self._partial = True
            self._warn(f"{rel}: became a non-regular file; restore is unavailable")
            self._write()
            return
        kind, digest, mode, _ = state
        entry.after_state = kind
        entry.after_hash = digest
        entry.after_mode = mode
        self._write()

    def note_shell(self, command: str) -> None:
        if self._turn_id is None:
            return
        text = " ".join(command.split())
        if not text:
            return
        if len(text) > _SHELL_COMMAND_CHARS:
            text = text[:_SHELL_COMMAND_CHARS] + "…"
        if text not in self._commands:
            self._commands.append(text)
        self._write()

    # ------------------------------------------------------------------ preview

    def preview(self, turn_ids: list[str] | tuple[str, ...]) -> CheckpointPreview:
        manifests = self._load_manifests(turn_ids)
        if not manifests:
            return CheckpointPreview()
        before, after, shell, commands, warnings, partial = self._merge(manifests)
        files: list[PreviewFile] = []
        restorable = not partial
        for rel in sorted(before):
            old = before[rel]
            new = after[rel]
            status = _status(old, new)
            if status is None:
                continue
            if old[0] == "present" and (
                old[1] is None or not self._blob_path(old[1]).is_file()
            ):
                restorable = False
                warnings.append(f"{rel}: checkpoint backup is missing")
            conflict = not self._current_matches(rel, new)
            files.append(PreviewFile(rel, status, conflict))
        return CheckpointPreview(
            files=tuple(files),
            shell_ran=shell,
            commands=tuple(commands),
            warnings=tuple(warnings),
            partial=partial,
            restorable=restorable,
        )

    def restore(
        self, turn_ids: list[str] | tuple[str, ...], *, force: bool = False
    ) -> RestoreResult:
        manifests = self._load_manifests(turn_ids)
        if not manifests:
            return RestoreResult()
        before, after, _, _, warnings, partial = self._merge(manifests)
        if partial:
            return RestoreResult(
                warnings=(*warnings, "checkpoint is partial"), aborted=True
            )
        files = [
            (rel, old, after[rel])
            for rel, old in sorted(before.items())
            if _status(old, after[rel]) is not None
        ]
        missing = [
            rel
            for rel, old, _ in files
            if old[0] == "present"
            and (old[1] is None or not self._blob_path(old[1]).is_file())
        ]
        if missing:
            return RestoreResult(
                skipped=tuple(missing),
                warnings=(
                    *warnings,
                    "one or more checkpoint backups are missing; nothing restored",
                ),
                aborted=True,
            )
        conflicts = [
            rel for rel, _, new in files if not self._current_matches(rel, new)
        ]
        if conflicts and not force:
            return RestoreResult(
                conflicts=tuple(conflicts),
                warnings=(*warnings, "refusing to overwrite files changed since the turn"),
                aborted=True,
            )
        restored: list[str] = []
        skipped: list[str] = []
        for rel, old, _ in files:
            target = self.workspace / rel
            try:
                if old[0] == "missing":
                    if target.is_symlink() or target.is_dir():
                        skipped.append(rel)
                        warnings.append(f"{rel}: non-regular path; skipped")
                        continue
                    if target.exists():
                        target.unlink()
                    restored.append(rel)
                    continue
                if old[1] is None:
                    skipped.append(rel)
                    warnings.append(f"{rel}: missing backup")
                    continue
                blob = self._blob_path(old[1])
                data = blob.read_bytes()
                if hashlib.sha256(data).hexdigest() != old[1]:
                    skipped.append(rel)
                    warnings.append(f"{rel}: checkpoint backup is corrupt")
                    continue
                if target.is_symlink() or target.is_dir():
                    skipped.append(rel)
                    warnings.append(f"{rel}: non-regular path; skipped")
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                atomic_write_bytes(target, data, mode=old[2] or 0o600)
                restored.append(rel)
            except OSError as exc:
                skipped.append(rel)
                warnings.append(f"{rel}: could not restore: {exc}")
        return RestoreResult(
            restored=tuple(restored),
            skipped=tuple(skipped),
            conflicts=tuple(conflicts) if force else (),
            warnings=tuple(warnings),
        )

    # ----------------------------------------------------------------- internals

    def _relative_path(self, raw_path: str) -> tuple[str, Path] | None:
        if not isinstance(raw_path, str) or not raw_path.strip():
            return None
        try:
            path = Path(raw_path).expanduser()
            if not path.is_absolute():
                path = self.workspace / path
            # Inspect the unresolved path first: a symlink must not silently
            # turn into a snapshot of its target.
            try:
                info = path.lstat()
            except FileNotFoundError:
                pass
            except OSError:
                return None
            else:
                if stat_module.S_ISLNK(info.st_mode) or not stat_module.S_ISREG(
                    info.st_mode
                ):
                    self._warn(
                        f"{raw_path}: symlink or non-regular file; not checkpointed"
                    )
                    return None
            path = path.resolve(strict=False)
            rel = path.relative_to(self.workspace)
        except (OSError, ValueError):
            key = str(raw_path)
            if key not in self._outside_warned:
                self._outside_warned.add(key)
                self._warn(f"{raw_path}: outside the workspace; not checkpointed")
            return None
        return rel.as_posix(), path

    def _read_state(
        self, path: Path
    ) -> tuple[str, str | None, int | None, bytes | None] | None:
        try:
            info = path.lstat()
        except FileNotFoundError:
            return ("missing", None, None, None)
        except OSError:
            return None
        if stat_module.S_ISLNK(info.st_mode) or not stat_module.S_ISREG(info.st_mode):
            return None
        try:
            data = path.read_bytes()
        except OSError:
            return None
        return (
            "present",
            hashlib.sha256(data).hexdigest(),
            stat_module.S_IMODE(info.st_mode),
            data,
        )

    def _current_matches(
        self, rel: str, expected: tuple[str, str | None, int | None]
    ) -> bool:
        state = self._read_state(self.workspace / rel)
        if state is None:
            return False
        kind, digest, _, _ = state
        if kind != expected[0]:
            return False
        return kind == "missing" or digest == expected[1]

    def _merge(
        self, manifests: list[_Manifest]
    ) -> tuple[
        dict[str, tuple[str, str | None, int | None]],
        dict[str, tuple[str, str | None, int | None]],
        bool,
        list[str],
        list[str],
        bool,
    ]:
        before: dict[str, tuple[str, str | None, int | None]] = {}
        after: dict[str, tuple[str, str | None, int | None]] = {}
        shell = False
        commands: list[str] = []
        warnings: list[str] = []
        partial = False
        for manifest in manifests:
            shell = shell or manifest.shell_ran
            commands.extend(command for command in manifest.commands if command not in commands)
            warnings.extend(manifest.warnings)
            partial = partial or manifest.partial
            for rel, entry in manifest.files.items():
                before.setdefault(
                    rel,
                    (entry.before_state, entry.before_hash, entry.before_mode),
                )
                after[rel] = (
                    entry.after_state,
                    entry.after_hash,
                    entry.after_mode,
                )
        return before, after, shell, commands, warnings, partial

    def _load_manifests(self, turn_ids: list[str] | tuple[str, ...]) -> list[_Manifest]:
        manifests: list[_Manifest] = []
        for turn_id in turn_ids:
            if not _TURN_ID.fullmatch(turn_id) or ".." in turn_id:
                continue
            path = self._manifest_path(turn_id)
            if not path.is_file():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                logger.warning("could not read checkpoint %s", turn_id)
                continue
            if not isinstance(data, dict):
                continue
            workspace = str(data.get("workspace", ""))
            if workspace and Path(workspace).resolve() != self.workspace:
                logger.warning("checkpoint workspace changed; skipping %s", turn_id)
                continue
            manifest = _Manifest(
                turn_id=turn_id,
                created_at=str(data.get("created_at", "")),
                workspace=workspace,
                shell_ran=bool(data.get("shell", {}).get("ran", False))
                if isinstance(data.get("shell"), dict)
                else False,
                commands=[
                    str(item)
                    for item in (
                        data.get("shell", {}).get("commands", [])
                        if isinstance(data.get("shell"), dict)
                        else []
                    )
                    if isinstance(item, str)
                ],
                warnings=[
                    str(item)
                    for item in data.get("warnings", [])
                    if isinstance(item, str)
                ],
                partial=bool(data.get("partial", False)),
            )
            raw_files = data.get("files", [])
            if isinstance(raw_files, list):
                for item in raw_files:
                    parsed = _entry_from_dict(item)
                    if parsed is not None:
                        manifest.files[parsed[0]] = parsed[1]
            manifests.append(manifest)
        return manifests

    def _write(self) -> None:
        if self._turn_id is None:
            return
        if not self._files and not self._commands and not self._warnings:
            return
        manifest = {
            "v": 1,
            "turn_id": self._turn_id,
            "created_at": datetime.now(UTC).isoformat(),
            "workspace": str(self.workspace),
            "shell": {"ran": bool(self._commands), "commands": list(self._commands)},
            "partial": self._partial,
            "warnings": list(self._warnings),
            "files": [
                {"path": rel, **entry.to_dict()}
                for rel, entry in sorted(self._files.items())
            ],
        }
        try:
            atomic_write_text(
                self._manifest_path(self._turn_id),
                json.dumps(manifest, ensure_ascii=False, indent=2),
            )
        except OSError:
            logger.warning("could not write checkpoint manifest", exc_info=True)

    def _warn(self, message: str) -> None:
        if message not in self._warnings:
            self._warnings.append(message)

    def _manifest_path(self, turn_id: str) -> Path:
        return self._turns_dir / f"{turn_id}.json"

    def _blob_path(self, digest: str) -> Path:
        return self._blobs_dir / digest[:2] / digest

    def _store_blob(self, digest: str, data: bytes) -> None:
        path = self._blob_path(digest)
        if path.is_file():
            return
        try:
            atomic_write_bytes(path, data)
        except OSError:
            logger.warning("could not write checkpoint blob", exc_info=True)

    def _prune(self) -> None:
        try:
            manifests = list(self._turns_dir.glob("*.json"))
        except OSError:
            return
        cutoff = time.time() - self.max_age_days * 86_400
        kept: list[tuple[float, Path]] = []
        for path in manifests:
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if mtime < cutoff:
                with contextlib.suppress(OSError):
                    path.unlink()
                continue
            kept.append((mtime, path))
        kept.sort(key=lambda item: item[0], reverse=True)
        for _, path in kept[self.max_checkpoints :]:
            with contextlib.suppress(OSError):
                path.unlink()
        kept = kept[: self.max_checkpoints]
        referenced: set[str] = set()
        for _, path in kept:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            for item in data.get("files", []) if isinstance(data, dict) else []:
                if not isinstance(item, dict):
                    continue
                before = item.get("before")
                if isinstance(before, dict) and isinstance(before.get("hash"), str):
                    referenced.add(before["hash"])
        try:
            blobs = list(self._blobs_dir.glob("*/*"))
        except OSError:
            return
        for blob in blobs:
            if blob.name not in referenced:
                with contextlib.suppress(OSError):
                    blob.unlink()


def _entry_from_dict(item: Any) -> tuple[str, _Entry] | None:
    if not isinstance(item, dict):
        return None
    rel = item.get("path")
    if not isinstance(rel, str) or not rel or rel.startswith("/") or ".." in Path(rel).parts:
        return None
    before = item.get("before")
    after = item.get("after")
    if not isinstance(before, dict) or not isinstance(after, dict):
        return None
    before_state = before.get("state")
    after_state = after.get("state")
    if before_state not in ("present", "missing") or after_state not in (
        "present",
        "missing",
    ):
        return None
    return (
        rel,
        _Entry(
            before_state=before_state,
            before_hash=_optional_str(before.get("hash")),
            before_mode=_optional_int(before.get("mode")),
            after_state=after_state,
            after_hash=_optional_str(after.get("hash")),
            after_mode=_optional_int(after.get("mode")),
        ),
    )


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _optional_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _status(
    before: tuple[str, str | None, int | None],
    after: tuple[str, str | None, int | None],
) -> str | None:
    if before[0] == "missing" and after[0] == "present":
        return "created"
    if before[0] == "present" and after[0] == "missing":
        return "deleted"
    if before[0] == "present" and after[0] == "present" and before[1] != after[1]:
        return "modified"
    return None
