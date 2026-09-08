"""Thread-safe admission and status for the one RCP hardware worker."""

from __future__ import annotations

import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from queue import Empty, Full, Queue
from threading import Lock

from .models import FirmwareRelease, PreparedFirmware

_MAX_OPERATION_LOG_ENTRIES = 64
_MAX_OPERATION_MESSAGE_LENGTH = 512


class OperationBusyError(RuntimeError):
    """A command was rejected because an RCP operation is already reserved."""


class OperationType(str, Enum):
    """The two execution modes understood by the main RCP worker."""

    INSTALL_LATEST = "install_latest"
    INSTALL_PREPARED = "install_prepared"


@dataclass(frozen=True)
class FlashCommand:
    """An immutable command admitted to the only hardware operation queue."""

    operation_type: OperationType
    source: str
    prepared: PreparedFirmware | None = None
    cleanup_path: Path | None = None

    def __post_init__(self) -> None:
        if self.operation_type is OperationType.INSTALL_LATEST:
            if self.prepared is not None or self.cleanup_path is not None:
                raise ValueError("an automatic update cannot include a prepared artifact")
        elif self.operation_type is OperationType.INSTALL_PREPARED:
            if self.prepared is None:
                raise ValueError("a prepared update requires a validated artifact")
        else:  # pragma: no cover - guarded by the enum type.
            raise ValueError("unsupported RCP operation")
        if self.source not in {"mqtt", "release", "url", "upload"}:
            raise ValueError("unsupported operation source")

    @classmethod
    def install_latest(cls) -> FlashCommand:
        return cls(OperationType.INSTALL_LATEST, "mqtt")

    @classmethod
    def install_prepared(
        cls, prepared: PreparedFirmware, cleanup_path: Path | None = None
    ) -> FlashCommand:
        return cls(OperationType.INSTALL_PREPARED, prepared.source, prepared, cleanup_path)


@dataclass(frozen=True)
class QueuedOperation:
    """A command plus an unguessable identifier used to reject stale updates."""

    identifier: str
    command: FlashCommand


@dataclass(frozen=True)
class RuntimeSnapshot:
    """The immutable, copied runtime view exposed to the Ingress API."""

    installed: dict[str, object]
    release: FirmwareRelease | None
    releases: tuple[FirmwareRelease, ...]
    manifest_error: str | None
    diagnostics: dict[str, object]


class OperationController:
    """Own one bounded command queue and all mutable operation/UI state.

    The controller lock protects reservation, lifecycle state, logs, and the
    runtime snapshot.  The main thread alone calls ``next`` and executes the
    returned command, while MQTT and Ingress threads can only submit commands.
    """

    def __init__(self) -> None:
        self._queue: Queue[QueuedOperation | None] = Queue(maxsize=1)
        self._lock = Lock()
        self._busy = False
        self._active_identifier: str | None = None
        self._state = "idle"
        self._stage = "idle"
        self._progress: int | None = None
        self._source: str | None = None
        self._error: str | None = None
        self._events: list[dict[str, str]] = []
        self._runtime = RuntimeSnapshot({}, None, (), None, {})

    def submit(self, command: FlashCommand) -> str:
        """Atomically reserve the sole queue slot or reject the new command."""

        with self._lock:
            if self._busy:
                raise OperationBusyError("an RCP operation is already pending or active")
            identifier = secrets.token_hex(16)
            operation = QueuedOperation(identifier, command)
            self._busy = True
            self._active_identifier = identifier
            self._state = "queued"
            self._stage = "Waiting for the hardware worker"
            self._progress = None
            self._source = command.source
            self._error = None
            self._events = []
            self._record_locked("Operation accepted")
            try:
                self._queue.put_nowait(operation)
            except Full as err:  # Defensive: busy and queue must stay in sync.
                self._reset_locked()
                raise OperationBusyError("the RCP operation queue is unavailable") from err
            return identifier

    def next(self, timeout: float) -> QueuedOperation | None:
        """Return the next admitted operation to the single main-thread worker."""

        try:
            return self._queue.get(timeout=timeout)
        except Empty:
            return None

    def wake(self) -> None:
        """Wake an idle worker during shutdown without replacing a real command."""

        try:
            self._queue.put_nowait(None)
        except Full:
            pass

    def start(self, operation: QueuedOperation) -> None:
        with self._lock:
            if not self._is_active_locked(operation.identifier):
                return
            self._state = "running"
            self._stage = "Validating firmware"
            self._record_locked("Hardware operation started")

    def report_progress(
        self, operation: QueuedOperation, percentage: int | None, stage: str
    ) -> None:
        if percentage is not None and not 0 <= percentage <= 100:
            raise ValueError("operation progress must be between 0 and 100")
        with self._lock:
            if not self._is_active_locked(operation.identifier):
                return
            self._state = "running"
            self._stage = self._safe_message(stage)
            self._progress = percentage
            self._record_locked(self._stage)

    def complete(self, operation: QueuedOperation, message: str = "Update verified") -> None:
        with self._lock:
            if not self._is_active_locked(operation.identifier):
                return
            self._state = "complete"
            self._stage = self._safe_message(message)
            self._progress = 100
            self._record_locked(self._stage)
            self._release_locked()

    def fail(self, operation: QueuedOperation, error: str) -> None:
        with self._lock:
            if not self._is_active_locked(operation.identifier):
                return
            self._state = "failed"
            self._stage = "Failed"
            self._error = self._safe_message(error)
            self._record_locked(self._error)
            self._release_locked()

    def cancel(self, operation: QueuedOperation, message: str) -> None:
        """Release a queued command that will not be executed after shutdown."""

        self.fail(operation, message)

    def operation_snapshot(self) -> dict[str, object]:
        """Return a JSON-safe copy without exposing command paths or credentials."""

        with self._lock:
            return {
                "busy": self._busy,
                "state": self._state,
                "stage": self._stage,
                "progress": self._progress,
                "source": self._source,
                "error": self._error,
                "events": [dict(event) for event in self._events],
            }

    def update_runtime(
        self,
        *,
        installed: Mapping[str, object],
        release: FirmwareRelease | None,
        releases: tuple[FirmwareRelease, ...],
        manifest_error: str | None,
        diagnostics: Mapping[str, object],
    ) -> None:
        """Publish copied, immutable state for HTTP readers without races."""

        with self._lock:
            self._runtime = RuntimeSnapshot(
                dict(installed),
                release,
                releases,
                self._safe_message(manifest_error) if manifest_error else None,
                dict(diagnostics),
            )

    def runtime_snapshot(self) -> RuntimeSnapshot:
        with self._lock:
            return RuntimeSnapshot(
                dict(self._runtime.installed),
                self._runtime.release,
                self._runtime.releases,
                self._runtime.manifest_error,
                dict(self._runtime.diagnostics),
            )

    def release_for(self, ncs_version: str) -> FirmwareRelease | None:
        with self._lock:
            return next(
                (
                    release
                    for release in self._runtime.releases
                    if release.ncs_version == ncs_version
                ),
                None,
            )

    def _is_active_locked(self, identifier: str) -> bool:
        return self._busy and self._active_identifier == identifier

    def _record_locked(self, message: str) -> None:
        self._events.append(
            {
                "time": datetime.now(UTC).isoformat(timespec="seconds"),
                "message": self._safe_message(message),
            }
        )
        del self._events[:-_MAX_OPERATION_LOG_ENTRIES]

    def _release_locked(self) -> None:
        self._busy = False
        self._active_identifier = None

    def _reset_locked(self) -> None:
        self._busy = False
        self._active_identifier = None
        self._state = "idle"
        self._stage = "idle"
        self._progress = None
        self._source = None
        self._error = None
        self._events = []

    @staticmethod
    def _safe_message(value: object) -> str:
        return str(value).replace("\x00", "?")[:_MAX_OPERATION_MESSAGE_LENGTH]
