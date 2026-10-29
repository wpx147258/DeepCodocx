"""Append-only JSONL session store with resume and a context checkpoint."""

from __future__ import annotations

import json
import logging
import shutil
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..atomic import append_line, atomic_write_text
from ..config import AppPaths
from ..deepdocx.profile import SessionProfile
from ..errors import SessionError
from ..messages import (
    Message,
    Usage,
    message_from_dict,
    message_to_dict,
    now_iso,
    usage_from_dict,
    usage_to_dict,
)
from .lock import FileLock
from .paths import SessionPaths, new_session_id, validate_session_id

logger = logging.getLogger(__name__)
SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class LoadedSession:
    session_id: str
    messages: tuple[Message, ...]
    provider: str | None
    model: str | None
    checkpoint: dict[str, Any] | None
    usage: Usage
    degraded: bool = False
    permission_mode: str | None = None
    auto_previous: str | None = None
    profile: SessionProfile = SessionProfile()


class SessionStore:
    def __init__(self, paths: SessionPaths, *, persist: bool = True) -> None:
        self.paths = paths
        self.persist = persist
        self._lock: FileLock | None = None
        self._ephemeral = False
        self.session_id = paths.root.name

    @classmethod
    def new(
        cls,
        app_paths: AppPaths,
        *,
        provider: str,
        model: str,
        cwd: Path,
        persist: bool = True,
    ) -> SessionStore:
        if persist:
            session_id = new_session_id()
            paths = SessionPaths.for_session(app_paths, session_id)
            store = cls(paths, persist=True)
            try:
                store._open_for_write(provider=provider, model=model, cwd=cwd)
            except BaseException:
                store.close()
                raise
            return store

        root = Path(tempfile.mkdtemp(prefix="cli-agent-session-"))
        paths = SessionPaths(
            root=root,
            jsonl=root / "session.jsonl",
            context=root / "context.json",
            meta=root / "meta.json",
            lock=root / "lock",
            spills=root / "spills",
            archive=root / "archive",
            checkpoints=root / "checkpoints",
        )
        store = cls(paths, persist=False)
        store._ephemeral = True
        return store

    @classmethod
    def resume(
        cls,
        app_paths: AppPaths,
        session_id: str,
        *,
        persist: bool = True,
    ) -> tuple[SessionStore, LoadedSession]:
        validate_session_id(session_id)
        paths = SessionPaths.for_session(app_paths, session_id)
        store = cls(paths, persist=persist)
        if persist:
            store._acquire_lock()
        try:
            loaded = store._load()
        except BaseException:
            store.close()
            raise
        return store, loaded

    @property
    def jsonl_path(self) -> Path:
        return self.paths.jsonl

    @property
    def context_path(self) -> Path:
        return self.paths.context

    @property
    def spills_dir(self) -> Path:
        return self.paths.spills

    def _open_for_write(self, *, provider: str, model: str, cwd: Path) -> None:
        self.paths.root.mkdir(parents=True, exist_ok=True)
        self._acquire_lock()
        record = {
            "v": SCHEMA_VERSION,
            "type": "session",
            "id": self.session_id,
            "project_id": self.paths.root.parent.parent.name,
            "cwd": str(cwd),
            "provider": provider,
            "model": model,
            "created_at": now_iso(),
        }
        self._append(record)
        self._write_meta(provider=provider, model=model, cwd=cwd)

    def _write_meta(self, *, provider: str, model: str, cwd: Path) -> None:
        meta = {
            "v": SCHEMA_VERSION,
            "session_id": self.session_id,
            "provider": provider,
            "model": model,
            "cwd": str(cwd),
        }
        try:
            atomic_write_text(
                self.paths.meta, json.dumps(meta, ensure_ascii=False, indent=2)
            )
        except OSError as exc:
            raise SessionError(f"could not write session metadata: {exc}") from exc

    def _acquire_lock(self) -> None:
        if self._lock is not None:
            return
        lock = FileLock(self.paths.lock)
        try:
            lock.acquire()
        except SessionError:
            raise
        self._lock = lock

    def append_message(self, message: Message) -> None:
        if not self.persist:
            return
        self._append(
            {
                "v": SCHEMA_VERSION,
                "type": "message",
                "message": message_to_dict(message),
            }
        )

    def append_usage(self, usage: Usage, model: str) -> None:
        if not self.persist:
            return
        self._append(
            {
                "v": SCHEMA_VERSION,
                "type": "usage",
                "model": model,
                **usage_to_dict(usage),
            }
        )

    def append_model(self, provider: str, model: str) -> None:
        if not self.persist:
            return
        self._append(
            {
                "v": SCHEMA_VERSION,
                "type": "model",
                "provider": provider,
                "model": model,
                "at": now_iso(),
            }
        )

    def append_clear(self) -> None:
        if not self.persist:
            return
        self._append({"v": SCHEMA_VERSION, "type": "clear", "at": now_iso()})

    def append_profile(self, profile: SessionProfile) -> None:
        if self.persist:
            self._append({"v": SCHEMA_VERSION, "type": "session_context", **profile.to_dict(), "at": now_iso()})
            meta = self._read_meta()
            meta["mode"] = profile.mode
            atomic_write_text(self.paths.meta, json.dumps(meta, ensure_ascii=False))

    def append_rewind(self, keep: int) -> None:
        """Persist a truncation of the transcript before ``keep`` messages."""
        if not self.persist:
            return
        self._append(
            {
                "v": SCHEMA_VERSION,
                "type": "rewind",
                "keep": max(0, int(keep)),
                "at": now_iso(),
            }
        )

    def append_permission_mode(self, mode: str, auto_previous: str | None) -> None:
        if not self.persist:
            return
        self._append(
            {
                "v": SCHEMA_VERSION,
                "type": "permission",
                "mode": mode,
                "auto_previous": auto_previous,
                "at": now_iso(),
            }
        )

    def save_context(self, checkpoint: dict[str, Any] | None) -> None:
        if not self.persist:
            return
        if checkpoint is None:
            try:
                self.paths.context.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise SessionError(
                    f"could not remove context checkpoint: {exc}"
                ) from exc
            return
        try:
            atomic_write_text(
                self.paths.context,
                json.dumps(
                    {"v": SCHEMA_VERSION, "last_compaction": checkpoint},
                    ensure_ascii=False,
                    indent=2,
                ),
            )
        except OSError as exc:
            raise SessionError(f"could not write context checkpoint: {exc}") from exc

    def load_context(self) -> dict[str, Any] | None:
        if not self.paths.context.is_file():
            return None
        try:
            data = json.loads(self.paths.context.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("could not read context checkpoint: %s", exc)
            return None
        if not isinstance(data, dict) or data.get("v") != SCHEMA_VERSION:
            return None
        checkpoint = data.get("last_compaction")
        return checkpoint if isinstance(checkpoint, dict) else None

    def _append(self, record: dict[str, Any]) -> None:
        try:
            append_line(self.paths.jsonl, json.dumps(record, ensure_ascii=False))
        except (OSError, UnicodeError) as exc:
            raise SessionError(
                f"could not append session record to {self.paths.jsonl}: {exc}"
            ) from exc

    def _load(self) -> LoadedSession:
        messages: list[Message] = []
        provider: str | None = None
        model: str | None = None
        usage = Usage()
        degraded = False
        permission_mode: str | None = None
        auto_previous: str | None = None
        profile = SessionProfile()
        if not self.paths.jsonl.is_file():
            return LoadedSession(
                session_id=self.session_id,
                messages=(),
                provider=provider,
                model=model,
                checkpoint=self.load_context(),
                usage=usage,
            )

        lines = self.paths.jsonl.read_text(encoding="utf-8").splitlines()
        for index, line in enumerate(lines):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                if index == len(lines) - 1:
                    # A torn final write is expected after a crash.
                    logger.warning("ignoring torn final session line")
                else:
                    logger.warning("skipping corrupt session line %d", index + 1)
                    degraded = True
                continue
            except (ValueError, OverflowError, RecursionError) as exc:
                # A complete line can still fail json.loads (for example an
                # integer beyond Python's digit limit). It is not a torn write.
                logger.warning("skipping unparseable session line %d: %s", index + 1, exc)
                degraded = True
                continue
            if not isinstance(record, dict):
                degraded = True
                continue
            if record.get("v") != SCHEMA_VERSION:
                logger.warning("ignoring unknown session record version: %r", record.get("v"))
                continue
            record_type = record.get("type")
            if record_type in ("session", "model"):
                raw_provider = record.get("provider")
                if raw_provider is not None:
                    if isinstance(raw_provider, str) and raw_provider:
                        provider = raw_provider
                    else:
                        logger.warning(
                            "ignoring invalid provider in session line %d", index + 1
                        )
                        degraded = True
                raw_model = record.get("model")
                if raw_model is not None:
                    if isinstance(raw_model, str) and raw_model:
                        model = raw_model
                    else:
                        logger.warning(
                            "ignoring invalid model in session line %d", index + 1
                        )
                        degraded = True
            elif record_type == "session_context":
                try:
                    profile = SessionProfile.from_dict(record)
                except (ValueError, TypeError):
                    degraded = True
            elif record_type == "message":
                raw_message = record.get("message")
                if not isinstance(raw_message, Mapping):
                    logger.warning(
                        "skipping invalid message record at line %d", index + 1
                    )
                    degraded = True
                    continue
                try:
                    messages.append(message_from_dict(raw_message))
                except (ValueError, TypeError, AttributeError) as exc:
                    logger.warning("skipping invalid message record: %s", exc)
                    degraded = True
            elif record_type == "usage":
                try:
                    usage = usage + usage_from_dict(record)
                except (ValueError, TypeError, OverflowError) as exc:
                    logger.warning(
                        "skipping invalid usage record at line %d: %s", index + 1, exc
                    )
                    degraded = True
            elif record_type == "rewind":
                keep = record.get("keep")
                if (
                    isinstance(keep, int)
                    and not isinstance(keep, bool)
                    and 0 <= keep <= len(messages)
                ):
                    del messages[keep:]
                else:
                    logger.warning(
                        "ignoring invalid rewind record at line %d", index + 1
                    )
                    degraded = True
            elif record_type == "clear":
                messages.clear()
            elif record_type == "permission":
                mode = record.get("mode")
                if isinstance(mode, str) and mode:
                    permission_mode = mode
                    previous = record.get("auto_previous")
                    auto_previous = previous if isinstance(previous, str) and previous else None
            else:
                logger.debug("ignoring unknown session record type: %r", record_type)

        if provider is None or model is None:
            meta = self._read_meta()
            meta_provider = meta.get("provider")
            meta_model = meta.get("model")
            if provider is None and isinstance(meta_provider, str) and meta_provider:
                provider = meta_provider
            if model is None and isinstance(meta_model, str) and meta_model:
                model = meta_model

        return LoadedSession(
            session_id=self.session_id,
            messages=tuple(messages),
            provider=provider,
            model=model,
            checkpoint=self.load_context(),
            usage=usage,
            degraded=degraded,
            permission_mode=permission_mode,
            auto_previous=auto_previous,
            profile=profile,
        )

    def _read_meta(self) -> dict[str, Any]:
        if not self.paths.meta.is_file():
            return {}
        try:
            data = json.loads(self.paths.meta.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def close(self) -> None:
        if self._lock is not None:
            self._lock.release()
            self._lock = None
        if self._ephemeral:
            shutil.rmtree(self.paths.root, ignore_errors=True)

    def __enter__(self) -> SessionStore:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()
