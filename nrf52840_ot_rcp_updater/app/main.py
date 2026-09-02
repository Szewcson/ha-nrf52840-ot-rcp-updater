"""Long-running Home Assistant app process."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from queue import Empty, SimpleQueue
from signal import SIGTERM, signal
from time import monotonic
from typing import NoReturn

from .manifest import FirmwareManifest, ManifestError
from .models import FirmwareRelease, Settings, ValidationError
from .mqtt_update import INSTALL_COMMAND, MqttUpdateEntity
from .state import StateError, StateStore
from .updater import RcpUpdater


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
LOGGER = logging.getLogger(__name__)


def _load_options() -> dict[str, object]:
    path = Path(os.environ.get("OT_RCP_OPTIONS", "/data/options.json"))
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as err:
        raise ValidationError(f"cannot load app options: {err}") from err
    if not isinstance(document, dict):
        raise ValidationError("app options must be a JSON object")
    return document


def _installed_state(store: StateStore) -> dict[str, object]:
    try:
        state = store.load()
    except StateError as err:
        LOGGER.error("Discarding unreadable persisted state: %s", err)
        return {}
    installed = state.get("installed")
    return installed if isinstance(installed, dict) else {}


def _load_latest(settings: Settings) -> tuple[FirmwareRelease | None, str | None]:
    if not settings.manifest_url:
        return None, "manifest_url is not configured"
    try:
        return FirmwareManifest.download(settings.manifest_url).newest_for(settings.hardware), None
    except ManifestError as err:
        return None, str(err)


def _environment_value(name: str, required: bool = True) -> str | None:
    value = os.environ.get(name)
    if value or not required:
        return value
    raise ValidationError(f"{name} is unavailable from the Home Assistant MQTT service")


def run() -> None:
    settings = Settings.from_mapping(_load_options())
    state_store = StateStore(Path(os.environ.get("OT_RCP_STATE_DIR", "/data")))
    commands: SimpleQueue[str] = SimpleQueue()
    mqtt_entity = MqttUpdateEntity(
        host=_environment_value("OT_RCP_MQTT_HOST") or "",
        port=int(_environment_value("OT_RCP_MQTT_PORT") or "1883"),
        username=_environment_value("OT_RCP_MQTT_USERNAME", required=False),
        password=_environment_value("OT_RCP_MQTT_PASSWORD", required=False),
        commands=commands,
    )
    updater = RcpUpdater(settings, state_store)
    running = True

    def stop(signum: int, frame: object) -> None:
        nonlocal running
        LOGGER.info("Received signal %s; stopping after the current operation", signum)
        running = False
        commands.put("STOP")

    signal(SIGTERM, stop)
    release, error = _load_latest(settings)
    mqtt_entity.start()
    mqtt_entity.publish_state(_installed_state(state_store), release, error=error)
    LOGGER.info("Published Home Assistant update entity")
    next_manifest_refresh = monotonic() + settings.manifest_poll_interval

    try:
        while running:
            timeout = max(0, next_manifest_refresh - monotonic())
            try:
                command = commands.get(timeout=timeout)
            except Empty:
                release, error = _load_latest(settings)
                mqtt_entity.publish_state(_installed_state(state_store), release, error=error)
                next_manifest_refresh = monotonic() + settings.manifest_poll_interval
                continue
            if command == "STOP":
                continue
            if command != INSTALL_COMMAND:
                continue

            release, error = _load_latest(settings)
            installed = _installed_state(state_store)
            if release is None:
                mqtt_entity.publish_state(installed, None, error=error)
                LOGGER.error("Rejecting install because no release is available: %s", error)
                next_manifest_refresh = monotonic() + settings.manifest_poll_interval
                continue

            mqtt_entity.publish_state(installed, release, in_progress=True)
            try:
                installed_ncp = updater.install(release)
                installed = {
                    "raw": installed_ncp.raw,
                    "hardware": installed_ncp.hardware,
                    "ncs_version": installed_ncp.ncs_version,
                    "zephyr_version": installed_ncp.zephyr_version,
                }
                mqtt_entity.publish_state(installed, release)
                LOGGER.info("Verified RCP at NCS %s", installed_ncp.ncs_version)
            except Exception as err:  # Keep the app alive after a controlled update failure.
                LOGGER.exception("RCP update failed")
                mqtt_entity.publish_state(installed, release, error=str(err))
            next_manifest_refresh = monotonic() + settings.manifest_poll_interval
    finally:
        mqtt_entity.stop()


def main() -> NoReturn:
    try:
        run()
    except (ValidationError, ValueError) as err:
        LOGGER.critical("Invalid app configuration: %s", err)
        raise SystemExit(1) from err
    raise SystemExit(0)


if __name__ == "__main__":
    main()
