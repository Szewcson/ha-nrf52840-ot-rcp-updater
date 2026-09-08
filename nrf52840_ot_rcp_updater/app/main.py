"""Long-running Home Assistant app process."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from signal import SIGTERM, signal
from time import monotonic
from typing import NoReturn

from .ingress import IngressApi, IngressServer
from .manifest import FirmwareManifest, ManifestError
from .models import (
    FIRMWARE_MANIFEST_URL,
    SUPPORTED_HARDWARE,
    FirmwareRelease,
    Settings,
    ValidationError,
    validate_version,
    version_key,
)
from .mqtt_update import MqttError, MqttUpdateEntity
from .operation import FlashCommand, OperationController, OperationType
from .spinel import SpinelError
from .state import StateError, StateStore
from .updater import RcpUpdater, RescanDeferred

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
LOGGER = logging.getLogger(__name__)
_MAX_OPTIONS_BYTES = 64 * 1024
_RETIRED_STATE_KEYS = (
    "completed_one_shot_ncs_version",
    "dfu_activity_at",
    # The old MQTT select/button pair persisted an exact target.  Retiring it
    # prevents a stale one-shot downgrade from surviving the Ingress migration.
    "selected_ncs_version",
)
_STARTUP_RESCAN_DELAY = 15
_RESCAN_RETRY_INTERVAL = 30
_TELEMETRY_RETRY_INTERVAL = 30


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


def _load_state(store: StateStore) -> dict[str, object]:
    try:
        return store.load()
    except StateError as err:
        LOGGER.error("Discarding unreadable persisted state: %s", err)
        return {}


def _prepare_state(store: StateStore, settings: Settings) -> dict[str, object]:
    """Discard persisted fields owned by retired manual MQTT controls."""

    del settings
    state = _load_state(store)
    updated = dict(state)
    for key in _RETIRED_STATE_KEYS:
        updated.pop(key, None)
    if updated != state:
        store.save(updated)
    return updated


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


def _select_release(
    manifest: FirmwareManifest, settings: Settings, state: dict[str, object]
) -> FirmwareRelease:
    """Choose the policy target; a lower target still needs an explicit install request."""

    del state
    release = manifest.newest_for(
        SUPPORTED_HARDWARE,
        allow_prereleases=settings.allow_prereleases,
        pinned_minor=settings.pinned_ncs_minor,
    )

    return release


def _policy_target_is_downgrade(release: FirmwareRelease, state: dict[str, object]) -> bool:
    """Identify an explicit channel/pin rollback from trusted live-or-verified state.

    Changing channel settings does not flash a radio by itself.  This marker is
    consumed only after Home Assistant sends an explicit ``update.install``
    command for that policy target.  Unknown state remains fail-closed; the
    Ingress release picker can still make an exact, deliberate selection.
    """

    installed = _installed_state(state)
    if not _has_trusted_installed_version(installed, SUPPORTED_HARDWARE):
        return False
    installed_version = installed.get("ncs_version")
    assert isinstance(installed_version, str)
    return version_key(release.ncs_version) < version_key(installed_version)


def _load_release(
    settings: Settings, state: dict[str, object]
) -> tuple[FirmwareManifest | None, FirmwareRelease | None, str | None]:
    try:
        manifest = FirmwareManifest.download(FIRMWARE_MANIFEST_URL)
        return manifest, _select_release(manifest, settings, state), None
    except ManifestError as err:
        return None, None, str(err)


def _available_releases(
    manifest: FirmwareManifest | None, settings: Settings
) -> tuple[FirmwareRelease, ...]:
    if manifest is None:
        return ()
    return manifest.releases_for(
        SUPPORTED_HARDWARE, allow_prereleases=settings.allow_prereleases
    )


def _publish_state(
    mqtt_entity: MqttUpdateEntity,
    state: dict[str, object],
    release: FirmwareRelease | None,
    error: str | None = None,
    in_progress: bool = False,
    update_percentage: float | None = None,
    progress_stage: str | None = None,
    diagnostics: dict[str, object] | None = None,
) -> None:
    mqtt_entity.publish_state(
        _installed_state(state),
        release,
        in_progress=in_progress,
        error=error,
        update_percentage=update_percentage,
        progress_stage=progress_stage,
        diagnostics=diagnostics,
    )


def _environment_value(name: str, required: bool = True) -> str | None:
    value = os.environ.get(name)
    if value or not required:
        return value
    raise ValidationError(f"{name} is unavailable from the Home Assistant MQTT service")


def run() -> None:
    settings = Settings.from_mapping(_load_options())
    state_store = StateStore(Path(os.environ.get("OT_RCP_STATE_DIR", "/data")))
    controller = OperationController()
    updater = RcpUpdater(settings, state_store)
    ingress_api = IngressApi(controller, updater)
    ingress_server = IngressServer(ingress_api)
    mqtt_entity = MqttUpdateEntity(
        host=_environment_value("OT_RCP_MQTT_HOST") or "",
        port=int(_environment_value("OT_RCP_MQTT_PORT") or "1883"),
        username=_environment_value("OT_RCP_MQTT_USERNAME", required=False),
        password=_environment_value("OT_RCP_MQTT_PASSWORD", required=False),
        submit_install=lambda: controller.submit(FlashCommand.install_latest()),
    )
    running = True

    def stop(signum: int, frame: object) -> None:
        del frame
        nonlocal running
        LOGGER.info("Received signal %s; stopping after the current operation", signum)
        running = False
        controller.wake()

    def publish(
        current_state: dict[str, object],
        current_release: FirmwareRelease | None,
        current_releases: tuple[FirmwareRelease, ...],
        manifest_error: str | None,
        *,
        error: str | None = None,
        in_progress: bool = False,
        update_percentage: float | None = None,
        progress_stage: str | None = None,
        diagnostics: dict[str, object] | None = None,
    ) -> dict[str, object]:
        observations = diagnostics if diagnostics is not None else updater.diagnostics()
        try:
            _publish_state(
                mqtt_entity,
                current_state,
                current_release,
                error=error,
                in_progress=in_progress,
                update_percentage=update_percentage,
                progress_stage=progress_stage,
                diagnostics=observations,
            )
        finally:
            controller.update_runtime(
                installed=_installed_state(current_state),
                release=current_release,
                releases=current_releases,
                manifest_error=manifest_error,
                diagnostics=observations,
            )
        return observations

    signal(SIGTERM, stop)
    state = _prepare_state(state_store, settings)
    manifest, release, manifest_error = _load_release(settings, state)
    available_releases = _available_releases(manifest, settings)

    try:
        mqtt_entity.start()
        publish(state, release, available_releases, manifest_error, error=manifest_error)
        ingress_server.start()
        LOGGER.info("Published Home Assistant update entity and started Ingress")
        next_manifest_refresh = monotonic() + settings.manifest_poll_interval
        next_rescan: float | None = monotonic() + _STARTUP_RESCAN_DELAY

        while running:
            deadlines = [next_manifest_refresh]
            if next_rescan is not None:
                deadlines.append(next_rescan)
            operation = controller.next(max(0, min(deadlines) - monotonic()))
            if operation is None:
                if not running:
                    break
                now = monotonic()
                if next_rescan is not None and now >= next_rescan:
                    state, completed = _rescan_installed_state(
                        state_store, state, settings, updater
                    )
                    next_rescan = None if completed else now + _RESCAN_RETRY_INTERVAL
                state = _prepare_state(state_store, settings)
                manifest, release, manifest_error = _load_release(settings, state)
                available_releases = _available_releases(manifest, settings)
                publish(
                    state,
                    release,
                    available_releases,
                    manifest_error,
                    error=manifest_error,
                )
                next_manifest_refresh = monotonic() + settings.manifest_poll_interval
                continue

            if not running:
                controller.cancel(operation, "Updater stopped before the queued operation started")
                ingress_api.discard_operation_artifact(operation.command)
                break

            controller.start(operation)
            try:
                state = _prepare_state(state_store, settings)
                manifest, release, manifest_error = _load_release(settings, state)
                available_releases = _available_releases(manifest, settings)
                operation_diagnostics = updater.diagnostics()

                def publish_progress(update_percentage: int | None, stage: str) -> None:
                    controller.report_progress(operation, update_percentage, stage)
                    publish(
                        state,
                        release,
                        available_releases,
                        manifest_error,
                        in_progress=True,
                        update_percentage=update_percentage,
                        progress_stage=stage,
                        diagnostics=operation_diagnostics,
                    )

                if operation.command.operation_type is OperationType.INSTALL_LATEST:
                    if release is None:
                        raise ManifestError(
                            manifest_error or "no release is available for the configured policy"
                        )
                    installed_ncp = updater.install(
                        release,
                        selected_target=_policy_target_is_downgrade(release, state),
                        progress=publish_progress,
                    )
                else:
                    package = operation.command.prepared
                    assert package is not None
                    installed_ncp = updater.install_prepared(
                        package, selected_target=True, progress=publish_progress
                    )
            except Exception as err:  # Keep the app alive after a controlled update failure.
                LOGGER.exception("RCP update failed")
                state = _mark_installed_unknown(state_store, state)
                controller.fail(operation, str(err))
                try:
                    publish(
                        state,
                        release,
                        available_releases,
                        manifest_error,
                        error=str(err),
                    )
                except MqttError:
                    LOGGER.exception("RCP update failed and MQTT error telemetry could not be published")
                next_rescan = monotonic() + _RESCAN_RETRY_INTERVAL
                next_manifest_refresh = monotonic() + settings.manifest_poll_interval
            else:
                # ``install`` and ``install_prepared`` commit only after a live
                # Spinel verification. Never erase that state because telemetry
                # publication fails later.
                state = _load_state(state_store)
                controller.complete(operation)
                try:
                    publish(state, release, available_releases, manifest_error)
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
                ingress_api.discard_operation_artifact(operation.command)
    finally:
        ingress_server.stop()
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
