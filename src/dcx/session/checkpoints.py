"""Per-turn file checkpoints for rewinding code alongside the transcript.

File-editing tools snapshot a file before their first write in a user turn.
The manifest keeps the pre-image (content-addressed under the session) and the
post-write hash, so a rewind can restore the workspace and detect files that
were changed manually since then. Shell side effects are recorded as a warning
only; they are not generally reversible.
"""

from __future__ import annotations

import contextlib
import difflib
import hashlib
import json
import logging
import re
import stat as stat_module
import time
from dataclasses import dataclass, field, replace
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
MAX_DIFF_BYTES = 64 * 1024
MAX_DIFF_LINES = 800
MAX_DIFF_INPUT_BYTES = 1024 * 1024
_SHELL_COMMAND_CHARS = 200
_TURN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")


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
class FileDiff:
    """What restoring one checkpointed file would change right now."""

    path: str
    status: str
    conflict: bool
    diff: str = ""
    truncated: bool = False
    binary: bool = False
    current_bytes: int | None = None
    restored_bytes: int | None = None
    warning: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "status": self.status,
            "conflict": self.conflict,
            "diff": self.diff,
            "truncated": self.truncated,
            "binary": self.binary,
            "current_bytes": self.current_bytes,
            "restored_bytes": self.restored_bytes,
            "warning": self.warning,
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
        self._files = {}
        self._commands = []
        self._warnings = []
        self._partial = False
        self._bytes = 0
        self._outside_warned = set()
        if not _TURN_ID.fullmatch(turn_id) or ".." in turn_id:
            logger.warning("ignoring invalid checkpoint turn id: %r", turn_id)
            self._turn_id = None
            return
        self._turn_id = turn_id
        load_warnings: list[str] = []
        try:
            existing = self._load_manifests([turn_id], load_warnings)
        except Exception:  # noqa: BLE001 - checkpointing is best effort
            logger.warning("could not load existing checkpoint", exc_info=True)
            existing = []
        if existing:
            manifest = existing[0]
            self._files = dict(manifest.files)
            self._commands = list(manifest.commands)
            self._warnings = [*manifest.warnings, *load_warnings]
            self._partial = manifest.partial
            for entry in self._files.values():
                if entry.before_hash:
                    with contextlib.suppress(OSError):
                        self._bytes += self._blob_path(entry.before_hash).stat().st_size
        else:
            self._warnings = list(load_warnings)
        try:
            self._prune(keep_turn_id=turn_id)
        except Exception:  # noqa: BLE001 - never fail a turn on maintenance
            logger.warning("checkpoint prune failed", exc_info=True)

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
        if info is not None and info.st_nlink > 1:
            self._warn(f"{rel}: hard-linked file; not checkpointed")
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
            self._write()
            return
        size = len(data) if data is not None else 0
        if size and self._bytes + size > self.max_turn_bytes:
            self._partial = True
            self._warn("turn checkpoint exceeded its 50 MiB limit")
            self._write()
            return
        if digest is not None and data is not None and not self._store_blob(digest, data):
            self._partial = True
            self._warn(f"{rel}: could not save the checkpoint backup")
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
        load_warnings: list[str] = []
        manifests = self._load_manifests(turn_ids, load_warnings)
        if not manifests:
            return CheckpointPreview(warnings=tuple(load_warnings))
        before, after, shell, commands, warnings, partial = self._merge(manifests)
        warnings.extend(load_warnings)
        files: list[PreviewFile] = []
        restorable = not partial
        for rel in sorted(before):
            old = before[rel]
            new = after[rel]
            status = _status(old, new)
            if status is None:
                continue
            if old[0] == "present" and not self._blob_ok(old[1]):
                restorable = False
                warnings.append(f"{rel}: checkpoint backup is missing or corrupt")
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

    def diff(self, turn_ids: list[str] | tuple[str, ...], path: str) -> FileDiff:
        """Return a bounded diff from the current file to the restore target.

        Only paths that belong to the checkpoint can be inspected, and the
        current file is read through the same containment checks used by
        ``restore``. Large or binary files return a warning instead of a diff.
        """
        load_warnings: list[str] = []
        manifests = self._load_manifests(turn_ids, load_warnings)
        if not manifests:
            raise ValueError("no checkpoint is available for this rewind")
        before, after, _, _, manifest_warnings, _ = self._merge(manifests)
        rel = path.strip()
        if rel not in before:
            raise ValueError("file is not part of this checkpoint")
        old = before[rel]
        new = after[rel]
        status = _status(old, new)
        if status is None:
            raise ValueError("file has no change in this checkpoint")
        conflict = not self._current_matches(rel, new)
        warning = "; ".join((*manifest_warnings, *load_warnings))

        current: bytes | None = None
        current_present = False
        current_known = True
        target = self._safe_target(rel)
        if target is None:
            current_known = False
            warning = _join_warning(warning, "current path is not safely readable")
        else:
            state = self._read_state(target)
            if state is None:
                current_known = False
                warning = _join_warning(
                    warning, "current file is not a regular file or is too large"
                )
            elif state[0] == "present":
                current_present = True
                current = state[3]

        restored: bytes | None = None
        if old[0] == "present":
            if old[1] is None or not _HASH.fullmatch(old[1]) or not self._blob_ok(old[1]):
                warning = _join_warning(
                    warning, "checkpoint backup is missing or corrupt"
                )
            else:
                try:
                    restored = self._blob_path(old[1]).read_bytes()
                except OSError:
                    warning = _join_warning(warning, "checkpoint backup could not be read")

        result = FileDiff(
            path=rel,
            status=status,
            conflict=conflict,
            current_bytes=len(current) if current is not None else None,
            restored_bytes=len(restored) if restored is not None else None,
            warning=warning,
        )
        if restored is None:
            if old[0] == "present":
                return result
            restored = b""
        if not current_known:
            return result
        if current is None:
            current = b""
        if len(current) > MAX_DIFF_INPUT_BYTES or len(restored) > MAX_DIFF_INPUT_BYTES:
            return replace(
                result,
                warning=_join_warning(
                    warning, "file is too large for a diff preview"
                ),
            )
        if b"\0" in current or b"\0" in restored:
            return replace(result, binary=True)
        if current == restored:
            return replace(
                result,
                diff="(contents already match the restore target; only the mode may change)",
            )

        diff_text, truncated = _unified_diff(
            current,
            restored,
            current_label="current" if current_present else "current (absent)",
            restored_label="restored" if old[0] == "present" else "restored (removed)",
        )
        return replace(result, diff=diff_text, truncated=truncated)

    def restore(
        self, turn_ids: list[str] | tuple[str, ...], *, force: bool = False
    ) -> RestoreResult:
        load_warnings: list[str] = []
        manifests = self._load_manifests(turn_ids, load_warnings)
        if not manifests:
            return RestoreResult(warnings=tuple(load_warnings), aborted=bool(load_warnings))
        before, after, _, _, warnings, partial = self._merge(manifests)
        warnings.extend(load_warnings)
        if partial or load_warnings:
            return RestoreResult(
                warnings=(*warnings, "checkpoint is partial or incomplete"),
                aborted=True,
            )
        files = [
            (rel, old, after[rel])
            for rel, old in sorted(before.items())
            if _status(old, after[rel]) is not None
        ]
        bad = [
            rel
            for rel, old, _ in files
            if old[0] == "present" and not self._blob_ok(old[1])
        ]
        if bad:
            return RestoreResult(
                skipped=tuple(bad),
                warnings=(
                    *warnings,
                    "one or more checkpoint backups are missing or corrupt; nothing restored",
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
        targets: dict[str, Path] = {}
        unsafe: list[str] = []
        for rel, _old, _ in files:
            target = self._safe_target(rel)
            if target is None:
                unsafe.append(rel)
                continue
            if target.is_symlink() or target.is_dir():
                unsafe.append(rel)
                continue
            targets[rel] = target
        if unsafe:
            return RestoreResult(
                skipped=tuple(unsafe),
                warnings=(
                    *warnings,
                    "one or more paths are not safely restorable; nothing restored",
                ),
                aborted=True,
            )
        restored: list[str] = []
        skipped: list[str] = []
        for rel, old, _ in files:
            target = targets[rel]
            try:
                if old[0] == "missing":
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
            aborted=bool(skipped),
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
        except (OSError, ValueError, RuntimeError):
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
        if info.st_nlink > 1 or info.st_size > self.max_file_bytes:
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

    def _safe_target(self, rel: str) -> Path | None:
        target = self.workspace / rel
        try:
            parent = target.parent.resolve(strict=True)
        except (OSError, RuntimeError):
            return None
        try:
            parent.relative_to(self.workspace)
        except ValueError:
            return None
        if target.is_symlink():
            return None
        return target

    def _blob_ok(self, digest: str | None) -> bool:
        if digest is None or not _HASH.fullmatch(digest):
            return False
        path = self._blob_path(digest)
        if not path.is_file():
            return False
        hasher = hashlib.sha256()
        try:
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    hasher.update(chunk)
        except OSError:
            return False
        return hasher.hexdigest() == digest

    def _current_matches(
        self, rel: str, expected: tuple[str, str | None, int | None]
    ) -> bool:
        target = self._safe_target(rel)
        if target is None:
            return False
        state = self._read_state(target)
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

    def _load_manifests(
        self,
        turn_ids: list[str] | tuple[str, ...],
        warnings: list[str] | None = None,
    ) -> list[_Manifest]:
        manifests: list[_Manifest] = []
        output = warnings if warnings is not None else []
        for turn_id in turn_ids:
            if not _TURN_ID.fullmatch(turn_id) or ".." in turn_id:
                continue
            path = self._manifest_path(turn_id)
            if not path.is_file():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                logger.warning("could not read checkpoint %s", turn_id)
                output.append(f"{turn_id}: checkpoint is unreadable")
                continue
            if not isinstance(data, dict):
                output.append(f"{turn_id}: checkpoint is invalid")
                continue
            workspace = str(data.get("workspace", ""))
            if workspace:
                try:
                    same_workspace = Path(workspace).resolve() == self.workspace
                except (OSError, RuntimeError):
                    same_workspace = False
                if not same_workspace:
                    logger.warning("checkpoint workspace changed; skipping %s", turn_id)
                    output.append(
                        f"{turn_id}: checkpoint was recorded for a different workspace"
                    )
                    continue
            shell = data.get("shell")
            shell = shell if isinstance(shell, dict) else {}
            raw_commands = shell.get("commands")
            raw_commands = raw_commands if isinstance(raw_commands, list) else []
            raw_warnings = data.get("warnings")
            raw_warnings = raw_warnings if isinstance(raw_warnings, list) else []
            manifest = _Manifest(
                turn_id=turn_id,
                created_at=str(data.get("created_at", "")),
                workspace=workspace,
                shell_ran=bool(shell.get("ran", False)),
                commands=[str(item) for item in raw_commands if isinstance(item, str)],
                warnings=[str(item) for item in raw_warnings if isinstance(item, str)],
                partial=bool(data.get("partial", False)),
            )
            raw_files = data.get("files")
            raw_files = raw_files if isinstance(raw_files, list) else []
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

    def _store_blob(self, digest: str, data: bytes) -> bool:
        path = self._blob_path(digest)
        if path.is_file():
            return True
        try:
            atomic_write_bytes(path, data)
        except OSError:
            logger.warning("could not write checkpoint blob", exc_info=True)
            return False
        return path.is_file()

    def mark_restored(self, turn_ids: list[str] | tuple[str, ...]) -> None:
        """After a successful restore, make retained manifests describe the restored state."""
        for turn_id in turn_ids:
            if not _TURN_ID.fullmatch(turn_id) or ".." in turn_id:
                continue
            path = self._manifest_path(turn_id)
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict):
                continue
            files = data.get("files")
            if not isinstance(files, list):
                continue
            for item in files:
                if not isinstance(item, dict):
                    continue
                before = item.get("before")
                if isinstance(before, dict):
                    item["after"] = {
                        "state": before.get("state"),
                        "hash": before.get("hash"),
                        "mode": before.get("mode"),
                    }
            try:
                atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2))
            except OSError:
                logger.warning("could not update checkpoint after restore", exc_info=True)

    def discard(self, turn_ids: list[str] | tuple[str, ...]) -> None:
        """Remove checkpoints for turns whose messages are no longer reachable."""
        for turn_id in turn_ids:
            if not _TURN_ID.fullmatch(turn_id) or ".." in turn_id:
                continue
            with contextlib.suppress(OSError):
                self._manifest_path(turn_id).unlink()
        try:
            self._prune()
        except Exception:  # noqa: BLE001 - best effort
            logger.warning("checkpoint prune failed", exc_info=True)

    def _prune(self, *, keep_turn_id: str | None = None) -> None:
        try:
            manifests = list(self._turns_dir.glob("*.json"))
        except OSError:
            return
        cutoff = time.time() - self.max_age_days * 86_400
        kept: list[tuple[int, Path]] = []
        for path in manifests:
            if keep_turn_id is not None and path.stem == keep_turn_id:
                try:
                    stat = path.stat()
                except OSError:
                    continue
                kept.append((stat.st_mtime_ns, path))
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            if stat.st_mtime < cutoff:
                with contextlib.suppress(OSError):
                    path.unlink()
                continue
            kept.append((stat.st_mtime_ns, path))
        kept.sort(key=lambda item: (item[0], item[1].name), reverse=True)
        keep_path = None
        kept_paths: list[Path] = []
        for _, path in kept:
            if keep_turn_id is not None and path.stem == keep_turn_id:
                keep_path = path
            kept_paths.append(path)
        kept_paths = kept_paths[: self.max_checkpoints]
        if keep_path is not None and keep_path not in kept_paths:
            kept_paths.append(keep_path)
        referenced: set[str] = set()
        for path in kept_paths:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            raw_files = data.get("files") if isinstance(data, dict) else []
            if not isinstance(raw_files, list):
                continue
            for item in raw_files:
                if not isinstance(item, dict):
                    continue
                before = item.get("before")
                if isinstance(before, dict) and isinstance(before.get("hash"), str):
                    referenced.add(before["hash"])
        keep_names = {path.name for path in kept_paths}
        for _, path in kept:
            if path.name not in keep_names:
                with contextlib.suppress(OSError):
                    path.unlink()
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
    if not isinstance(rel, str) or not rel:
        return None
    parsed_path = Path(rel)
    if (
        parsed_path.is_absolute()
        or parsed_path.drive
        or parsed_path.anchor
        or ".." in parsed_path.parts
    ):
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
    before_hash = _optional_hash(before.get("hash"))
    after_hash = _optional_hash(after.get("hash"))
    if before_state == "present" and before_hash is None:
        return None
    if after_state == "present" and after_hash is None:
        return None
    return (
        rel,
        _Entry(
            before_state=before_state,
            before_hash=before_hash,
            before_mode=_optional_int(before.get("mode")),
            after_state=after_state,
            after_hash=after_hash,
            after_mode=_optional_int(after.get("mode")),
        ),
    )


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _optional_hash(value: Any) -> str | None:
    return value if isinstance(value, str) and _HASH.fullmatch(value) else None


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
    if before[0] == "present" and after[0] == "present" and (
        before[1] != after[1] or before[2] != after[2]
    ):
        return "modified"
    return None


def _join_warning(existing: str, extra: str) -> str:
    return f"{existing}; {extra}" if existing else extra


def _unified_diff(
    current: bytes,
    restored: bytes,
    *,
    current_label: str,
    restored_label: str,
) -> tuple[str, bool]:
    """Diff two bounded texts, stopping at the output limits."""
    current_text = current.decode("utf-8", errors="replace")
    restored_text = restored.decode("utf-8", errors="replace")
    chunks: list[str] = []
    size = 0
    truncated = False
    for line in difflib.unified_diff(
        current_text.splitlines(),
        restored_text.splitlines(),
        fromfile=current_label,
        tofile=restored_label,
        lineterm="",
        n=3,
    ):
        if len(chunks) >= MAX_DIFF_LINES or size + len(line) + 1 > MAX_DIFF_BYTES:
            truncated = True
            break
        chunks.append(line)
        size += len(line) + 1
    if not chunks:
        return (
            "(contents differ but no textual lines changed; "
            "the mode or line endings may differ)",
            False,
        )
    text = "\n".join(chunks)
    if truncated:
        text += "\n… diff truncated"
    return text, truncated
