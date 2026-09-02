"""Crash-safe state persistence and an inter-process update lock."""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import tempfile
from typing import Iterator


class StateError(RuntimeError):
    """Persistent updater state cannot be read or safely changed."""


class StateStore:
    """Keeps only the last verified RCP metadata for non-disruptive UI updates."""

    def __init__(self, directory: Path) -> None:
        self._directory = directory
        self._path = directory / "state.json"

    @property
    def directory(self) -> Path:
        """Directory shared by the state file, lock, and verified downloads."""

        return self._directory

    def load(self) -> dict[str, object]:
        if not self._path.exists():
            return {}
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as err:
            raise StateError(f"cannot load updater state: {err}") from err
        if not isinstance(payload, dict):
            raise StateError("updater state is not a JSON object")
        return payload

    def save(self, state: dict[str, object]) -> None:
        self._directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix="state-", suffix=".json", dir=self._directory)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(state, stream, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(name, 0o600)
            os.replace(name, self._path)
            directory_descriptor = os.open(self._directory, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        finally:
            try:
                os.unlink(name)
            except FileNotFoundError:
                pass


@contextmanager
def update_lock(directory: Path) -> Iterator[None]:
    """Allow one updater process across restarts and duplicate MQTT commands."""

    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / "update.lock"
    with path.open("a+", encoding="utf-8") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as err:
            raise StateError("a firmware update is already in progress") from err
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
