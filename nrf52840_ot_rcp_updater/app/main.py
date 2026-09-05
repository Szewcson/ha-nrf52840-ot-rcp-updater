"""Long-running Home Assistant app process."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from queue import Empty, Full, Queue
from signal import SIGTERM, signal
from threading import Event
from time import monotonic
from typing import NoReturn

from .manifest import FirmwareManifest, ManifestError
from .models import (
    FIRMWARE_MANIFEST_URL,
    SUPPORTED_HARDWARE,
    FirmwareRelease,
    Settings,
    ValidationError,
    is_prerelease,
    validate_version,
    version_key,
)
from .mqtt_update import (
    AUTOMATIC_TARGET,
    FLASH_SELECTED_COMMAND,
    INSTALL_COMMAND,
    SELECT_TARGET_COMMAND_PREFIX,
    MqttError,
    MqttUpdateEntity,
)
from .spinel import SpinelError
from .state import StateError, StateStore
from .updater import RcpUpdater, RescanDeferred

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
LOGGER = logging.getLogger(__name__)
_MAX_OPTIONS_BYTES = 64 * 1024


def _load_options() -> dict[str, object]:
    path = Path(os.environ.get("OT_RCP_OPTIONS", "/data/options.json"))
    try:
        # Supervisor options are compact. Bound this persisted input before
        # decoding so a corrupted file cannot consume arbitrary memory.
        if path.stat().st_size > _MAX_OPTIONS_BYTES:
            raise ValidationError(f"app options exceed {_MAX_OPTIONS_BYTES} bytes")
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as err:
        raise ValidationError(f"cannot load app options: {err}") from err
    if not isinstance(document, dict):
        raise ValidationError("app options must be a JSON object")
    return document


_SELECTED_TARGET = "selected_ncs_version"
_RETIRED_STATE_KEYS = ("completed_one_shot_ncs_version", "dfu_activity_at")
_STARTUP_RESCAN_DELAY = 15
_RESCAN_RETRY_INTERVAL = 30
_TELEMETRY_RETRY_INTERVAL = 30


def _load_state(store: StateStore) -> dict[str, object]:
    try:
        return store.load()
    except StateError as err:
        LOGGER.error("Discarding unreadable persisted state: %s", err)
        return {}


def _prepare_state(store: StateStore, settings: Settings) -> dict[str, object]:
    """Discard an invalid persisted manual target."""

    state = _load_state(store)
    changed = False
    for key in _RETIRED_STATE_KEYS:
        if key in state:
            state = dict(state)
            state.pop(key)
            changed = True
    if _SELECTED_TARGET in state and _selected_target(state) is None:
        state = dict(state)
        state.pop(_SELECTED_TARGET)
        changed = True
    if changed:
        store.save(state)
    return state


def _installed_state(state: dict[str, object]) -> dict[str, object]:
    installed = state.get("installed")
    return installed if isinstance(installed, dict) else {}


def _has_trusted_installed_version(installed: dict[str, object], hardware: str) -> bool:
    """Only state persisted after post-flash Spinel verification is trusted here."""

    ncs_version = installed.get("ncs_version")
    zephyr_version = installed.get("zephyr_version")
    if installed.get("hardware") != hardware:
        return False
    try:
        validate_version(ncs_version, "installed ncs_version")
        validate_version(zephyr_version, "installed zephyr_version")
    except ValidationError:
        return False
    return True


def _mark_installed_unknown(store: StateStore, state: dict[str, object]) -> dict[str, object]:
    """Discard a stale identity after a failed or unverified firmware operation."""

    updated = dict(state)
    changed = False
    for key in ("installed", "verified_at"):
        if key in updated:
            updated.pop(key)
            changed = True
    if changed:
        store.save(updated)
    return updated


def _rescan_installed_state(
    store: StateStore, state: dict[str, object], settings: Settings, updater: RcpUpdater
) -> tuple[dict[str, object], bool]:
    """Refresh trusted state from Spinel, deferring while OTBR is not ready."""

    try:
        version = updater.current_version()
    except RescanDeferred as err:
        LOGGER.info("Deferring RCP version rescan: %s", err)
        return state, False
    except (SpinelError, StateError) as err:
        LOGGER.warning("Unable to rescan installed RCP firmware: %s", err)
        return _mark_installed_unknown(store, state), True

    installed = asdict(version)
    if not _has_trusted_installed_version(installed, SUPPORTED_HARDWARE):
        LOGGER.warning("RCP rescan did not return complete matching HW/NCS/ZEPHYR tags")
        return _mark_installed_unknown(store, state), True

    updated = dict(state)
    updated["installed"] = installed
    updated["verified_at"] = datetime.now(UTC).isoformat()
    store.save(updated)
    LOGGER.info("Rescanned RCP at NCS %s", version.ncs_version)
    return updated, True


def _selected_target(state: dict[str, object]) -> str | None:
    """Return the manifest selector value only when persisted state is valid."""

    value = state.get(_SELECTED_TARGET)
    try:
        return validate_version(value, "selected firmware target")
    except ValidationError:
        return None


def _select_release(
    manifest: FirmwareManifest, settings: Settings, state: dict[str, object]
) -> FirmwareRelease:
    installed = _installed_state(state)
    selected_target = _selected_target(state)
    if selected_target is not None:
        return manifest.release_for(SUPPORTED_HARDWARE, selected_target)

    release = manifest.newest_for(
        SUPPORTED_HARDWARE,
        allow_prereleases=settings.allow_prereleases,
        pinned_minor=settings.pinned_ncs_minor,
    )
    if _has_trusted_installed_version(installed, SUPPORTED_HARDWARE):
        installed_version = installed.get("ncs_version")
        assert isinstance(installed_version, str)
        if version_key(release.ncs_version) < version_key(installed_version):
            raise ManifestError(
                "configured NCS policy would downgrade the RCP; "
                "select an exact manual firmware target to confirm it"
            )
    return release


def _load_release(
    settings: Settings, state: dict[str, object]
) -> tuple[FirmwareManifest | None, FirmwareRelease | None, str | None]:
    try:
        manifest = FirmwareManifest.download(FIRMWARE_MANIFEST_URL)
        return manifest, _select_release(manifest, settings, state), None
    except ManifestError as err:
        return None, None, str(err)


def _target_versions(manifest: FirmwareManifest | None, settings: Settings) -> tuple[str, ...]:
    if manifest is None:
        return ()
    return tuple(
        release.ncs_version
        for release in manifest.releases_for(
            SUPPORTED_HARDWARE, allow_prereleases=settings.allow_prereleases
        )
    )


def _set_selected_target(
    store: StateStore,
    state: dict[str, object],
    manifest: FirmwareManifest,
    settings: Settings,
    target: str,
) -> dict[str, object]:
    """Persist a user-selected manifest release as the next explicit target."""

    updated = dict(state)
    if target == AUTOMATIC_TARGET:
        updated.pop(_SELECTED_TARGET, None)
    else:
        release = manifest.release_for(SUPPORTED_HARDWARE, target)
        if not settings.allow_prereleases and is_prerelease(release.ncs_version):
            raise ManifestError("allow_prereleases is required to select a preview or RC release")
        updated[_SELECTED_TARGET] = release.ncs_version
    store.save(updated)
    return updated


def _clear_selected_target(store: StateStore) -> dict[str, object]:
    state = _load_state(store)
    if _SELECTED_TARGET in state:
        state = dict(state)
        state.pop(_SELECTED_TARGET)
        store.save(state)
    return state


def _return_to_automatic_target(
    store: StateStore, manifest: FirmwareManifest, settings: Settings
) -> tuple[dict[str, object], FirmwareRelease]:
    """Clear a verified manual request and immediately restore normal release policy."""

    state = _clear_selected_target(store)
    return state, _select_release(manifest, settings, state)


def _publish_state(
    mqtt_entity: MqttUpdateEntity,
    updater: RcpUpdater,
    state: dict[str, object],
    release: FirmwareRelease | None,
    error: str | None = None,
    in_progress: bool = False,
    update_percentage: float | None = None,
    progress_stage: str | None = None,
    target_versions: tuple[str, ...] = (),
    diagnostics: dict[str, object] | None = None,
) -> None:
    mqtt_entity.publish_state(
        _installed_state(state),
        release,
        in_progress=in_progress,
        error=error,
        update_percentage=update_percentage,
        progress_stage=progress_stage,
        target_versions=target_versions,
        selected_target=_selected_target(state),
        diagnostics=diagnostics if diagnostics is not None else updater.diagnostics(),
    )


def _environment_value(name: str, required: bool = True) -> str | None:
    value = os.environ.get(name)
    if value or not required:
        return value
    raise ValidationError(f"{name} is unavailable from the Home Assistant MQTT service")


def run() -> None:
    settings = Settings.from_mapping(_load_options())
    state_store = StateStore(Path(os.environ.get("OT_RCP_STATE_DIR", "/data")))
    commands: Queue[str] = Queue(maxsize=1)
    # The MQTT callback reserves this before queueing a flash command, closing
    # the gap between command dequeueing and the start of the hardware update.
    operation_busy = Event()
    mqtt_entity = MqttUpdateEntity(
        host=_environment_value("OT_RCP_MQTT_HOST") or "",
        port=int(_environment_value("OT_RCP_MQTT_PORT") or "1883"),
        username=_environment_value("OT_RCP_MQTT_USERNAME", required=False),
        password=_environment_value("OT_RCP_MQTT_PASSWORD", required=False),
        commands=commands,
        operation_busy=operation_busy,
    )
    updater = RcpUpdater(settings, state_store)
    running = True

    def stop(signum: int, frame: object) -> None:
        nonlocal running
        LOGGER.info("Received signal %s; stopping after the current operation", signum)
        running = False
        try:
            commands.put_nowait("STOP")
        except Full:
            # A queued command will wake the loop, which checks ``running`` before acting.
            pass

    signal(SIGTERM, stop)
    state = _prepare_state(state_store, settings)
    manifest, release, error = _load_release(settings, state)
    target_versions = _target_versions(manifest, settings)
    mqtt_entity.start()
    _publish_state(mqtt_entity, updater, state, release, error, target_versions=target_versions)
    LOGGER.info("Published Home Assistant update entity")
    next_manifest_refresh = monotonic() + settings.manifest_poll_interval
    next_rescan: float | None = monotonic() + _STARTUP_RESCAN_DELAY

    try:
        while running:
            deadlines = [next_manifest_refresh]
            if next_rescan is not None:
                deadlines.append(next_rescan)
            timeout = max(0, min(deadlines) - monotonic())
            try:
                command = commands.get(timeout=timeout)
            except Empty:
                now = monotonic()
                if next_rescan is not None and now >= next_rescan:
                    state, completed = _rescan_installed_state(
                        state_store, state, settings, updater
                    )
                    next_rescan = None if completed else now + _RESCAN_RETRY_INTERVAL
                    manifest, release, error = _load_release(settings, state)
                    target_versions = _target_versions(manifest, settings)
                    _publish_state(
                        mqtt_entity, updater, state, release, error, target_versions=target_versions
                    )
                    continue
                state = _prepare_state(state_store, settings)
                manifest, release, error = _load_release(settings, state)
                target_versions = _target_versions(manifest, settings)
                _publish_state(
                    mqtt_entity, updater, state, release, error, target_versions=target_versions
                )
                next_manifest_refresh = monotonic() + settings.manifest_poll_interval
                continue
            if command == "STOP" or not running:
                operation_busy.clear()
                continue
            if command.startswith(SELECT_TARGET_COMMAND_PREFIX):
                target = command.removeprefix(SELECT_TARGET_COMMAND_PREFIX)
                state = _prepare_state(state_store, settings)
                manifest, release, error = _load_release(settings, state)
                if manifest is None:
                    _publish_state(mqtt_entity, updater, state, None, error)
                    LOGGER.error(
                        "Rejecting target selection because no manifest is available: %s", error
                    )
                    continue
                try:
                    state = _set_selected_target(state_store, state, manifest, settings, target)
                    release = _select_release(manifest, settings, state)
                    error = None
                except ManifestError as err:
                    error = str(err)
                target_versions = _target_versions(manifest, settings)
                _publish_state(
                    mqtt_entity,
                    updater,
                    state,
                    release if error is None else None,
                    error,
                    target_versions=target_versions,
                )
                continue
            if command not in (INSTALL_COMMAND, FLASH_SELECTED_COMMAND):
                continue

            state = _prepare_state(state_store, settings)
            manifest, release, error = _load_release(settings, state)
            target_versions = _target_versions(manifest, settings)
            if command == FLASH_SELECTED_COMMAND and _selected_target(state) is None:
                operation_busy.clear()
                error = "choose a Manual RCP firmware target before flashing it"
                _publish_state(
                    mqtt_entity, updater, state, release, error, target_versions=target_versions
                )
                LOGGER.error("Rejecting manual flash because no target is selected")
                continue
            if release is None:
                operation_busy.clear()
                _publish_state(
                    mqtt_entity, updater, state, None, error, target_versions=target_versions
                )
                LOGGER.error("Rejecting install because no release is available: %s", error)
                next_manifest_refresh = monotonic() + settings.manifest_poll_interval
                continue

            selected_target = _selected_target(state) == release.ncs_version
            operation_diagnostics = updater.diagnostics()

            def publish_progress(
                update_percentage: int,
                stage: str,
                current_state: dict[str, object] = state,
                current_release: FirmwareRelease = release,
                current_target_versions: tuple[str, ...] = target_versions,
                current_diagnostics: dict[str, object] = operation_diagnostics,
            ) -> None:
                _publish_state(
                    mqtt_entity,
                    updater,
                    current_state,
                    current_release,
                    in_progress=True,
                    update_percentage=update_percentage,
                    progress_stage=stage,
                    target_versions=current_target_versions,
                    diagnostics=current_diagnostics,
                )

            try:
                installed_ncp = updater.install(
                    release, selected_target=selected_target, progress=publish_progress
                )
            except Exception as err:  # Keep the app alive after a controlled update failure.
                LOGGER.exception("RCP update failed")
                state = _mark_installed_unknown(state_store, state)
                _publish_state(
                    mqtt_entity,
                    updater,
                    state,
                    release,
                    str(err),
                    target_versions=target_versions,
                )
                next_rescan = monotonic() + _RESCAN_RETRY_INTERVAL
                next_manifest_refresh = monotonic() + settings.manifest_poll_interval
            else:
                # ``install`` has committed a post-flash Spinel verification. Do not
                # turn that successful physical transaction into "unknown" because
                # optional MQTT telemetry or manual-target cleanup has a later fault.
                state = _load_state(state_store)
                post_install_error: str | None = None
                if selected_target:
                    assert manifest is not None
                    try:
                        state, release = _return_to_automatic_target(
                            state_store, manifest, settings
                        )
                    except (ManifestError, StateError) as err:
                        post_install_error = str(err)
                try:
                    _publish_state(
                        mqtt_entity,
                        updater,
                        state,
                        release,
                        error=post_install_error,
                        target_versions=target_versions,
                    )
                except MqttError:
                    LOGGER.exception(
                        "RCP update verified, but final MQTT publication failed; "
                        "keeping the verified installed state"
                    )
                    next_manifest_refresh = monotonic() + _TELEMETRY_RETRY_INTERVAL
                else:
                    next_manifest_refresh = monotonic() + settings.manifest_poll_interval
                LOGGER.info("Verified RCP at NCS %s", installed_ncp.ncs_version)
                next_rescan = None
            finally:
                operation_busy.clear()
    finally:
        mqtt_entity.stop()


def main() -> NoReturn:
    try:
        run()
    except (MqttError, ValidationError, ValueError) as err:
        LOGGER.critical("Updater stopped: %s", err)
        raise SystemExit(1) from err
    raise SystemExit(0)


if __name__ == "__main__":
    main()
