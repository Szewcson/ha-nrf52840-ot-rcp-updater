"""Home Assistant MQTT Discovery transport for a native update entity."""

from __future__ import annotations

import json
import logging
from queue import Full, Queue
from threading import Event, Lock
from typing import Any

try:
    import paho.mqtt.client as mqtt
except ImportError:  # pragma: no cover - the container provides paho-mqtt.
    mqtt = None

from .models import FirmwareRelease

LOGGER = logging.getLogger(__name__)
BASE_TOPIC = "nrf52840_ot_rcp_updater/rcp"
DISCOVERY_TOPIC = "homeassistant/update/nrf52840_ot_rcp_updater/rcp/config"
STATE_TOPIC = f"{BASE_TOPIC}/state"
COMMAND_TOPIC = f"{BASE_TOPIC}/set"
AVAILABILITY_TOPIC = f"{BASE_TOPIC}/availability"
ATTRIBUTES_TOPIC = f"{BASE_TOPIC}/attributes"
TARGET_DISCOVERY_TOPIC = "homeassistant/select/nrf52840_ot_rcp_updater/firmware_target/config"
TARGET_STATE_TOPIC = f"{BASE_TOPIC}/firmware_target/state"
TARGET_COMMAND_TOPIC = f"{BASE_TOPIC}/firmware_target/set"
MANUAL_FLASH_DISCOVERY_TOPIC = (
    "homeassistant/button/nrf52840_ot_rcp_updater/flash_selected_firmware/config"
)
MANUAL_FLASH_COMMAND_TOPIC = f"{BASE_TOPIC}/flash_selected_firmware/set"
INSTALL_COMMAND = "INSTALL"
FLASH_SELECTED_COMMAND = "FLASH_SELECTED"
_HARDWARE_OPERATION_COMMANDS = frozenset((INSTALL_COMMAND, FLASH_SELECTED_COMMAND))
SELECT_TARGET_COMMAND_PREFIX = "SELECT_TARGET:"
AUTOMATIC_TARGET = "Automatic"
# The longest accepted wire value is a manually selected NCS version (80 bytes).
_MAX_COMMAND_BYTES = 80
DISCOVERY_ORIGIN = {
    "name": "ha-nrf52840-ot-rcp-updater",
    "support_url": "https://github.com/Szewcson/ha-nrf52840-ot-rcp-updater",
}


class MqttError(RuntimeError):
    """The Supervisor-provided MQTT service could not be used."""


def update_state_payload(
    installed: dict[str, object],
    release: FirmwareRelease | None,
    in_progress: bool,
    error: str | None,
    update_percentage: float | None = None,
    progress_stage: str | None = None,
) -> dict[str, object]:
    """Build the documented JSON state consumed by Home Assistant Update."""

    installed_ncs = installed.get("ncs_version")
    installed_version = installed_ncs if isinstance(installed_ncs, str) else "unknown"
    latest_version = release.ncs_version if release else installed_version
    summary = release.release_summary if release else "No release manifest is configured."
    summary = f"Installed RCP: NCS {installed_version}\n\n{summary}"
    if progress_stage:
        summary = f"{summary}\nUpdate stage: {progress_stage}"[:255]
    if error:
        summary = f"{summary}\nLast updater error: {error}"[:255]
    title = (
        f"Available firmware: NCS {release.ncs_version} / Zephyr {release.zephyr_version}"
        if release
        else "No RCP release available"
    )
    return {
        "installed_version": installed_version,
        "latest_version": latest_version,
        "title": title,
        "release_url": release.release_url if release else "",
        "release_summary": summary,
        "in_progress": in_progress,
        # MQTT Update uses null to clear a prior percentage when work ends.
        "update_percentage": update_percentage if in_progress else None,
    }


class MqttUpdateEntity:
    """Publishes Home Assistant MQTT discovery for updates, target selection, and diagnostics."""

    def __init__(
        self,
        host: str,
        port: int,
        username: str | None,
        password: str | None,
        commands: Queue[str],
        connect_timeout: float = 30.0,
        operation_busy: Event | None = None,
    ) -> None:
        if mqtt is None:
            raise MqttError("paho-mqtt is unavailable in this app image")
        if connect_timeout <= 0:
            raise ValueError("connect_timeout must be positive")
        self._commands = commands
        # Set for both a queued and executing flash operation. The lock makes
        # the state check and queue reservation one indivisible transition.
        self._operation_busy = operation_busy if operation_busy is not None else Event()
        self._command_lock = Lock()
        self._connected = Event()
        self._connect_timeout = connect_timeout
        self._client = mqtt.Client(client_id="nrf52840-ot-rcp-updater", clean_session=True)
        if username:
            self._client.username_pw_set(username, password)
        self._client.will_set(AVAILABILITY_TOPIC, "offline", qos=1, retain=True)
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._host = host
        self._port = port

    def start(self) -> None:
        try:
            self._client.connect_async(self._host, self._port, keepalive=60)
            self._client.loop_start()
        except OSError as err:
            raise MqttError(f"unable to connect to MQTT service: {err}") from err
        if self._connected.wait(self._connect_timeout):
            return
        self._client.loop_stop()
        self._client.disconnect()
        raise MqttError("timed out waiting for the Home Assistant MQTT service")

    def stop(self) -> None:
        if self._connected.is_set():
            self._client.publish(AVAILABILITY_TOPIC, "offline", qos=1, retain=True)
        self._client.loop_stop()
        self._client.disconnect()
        self._connected.clear()

    def publish_state(
        self,
        installed: dict[str, object],
        release: FirmwareRelease | None,
        in_progress: bool = False,
        error: str | None = None,
        update_percentage: float | None = None,
        progress_stage: str | None = None,
        target_versions: tuple[str, ...] = (),
        selected_target: str | None = None,
        diagnostics: dict[str, object] | None = None,
    ) -> None:
        payload = update_state_payload(
            installed,
            release,
            in_progress,
            error,
            update_percentage=update_percentage,
            progress_stage=progress_stage,
        )
        self._publish_json(STATE_TOPIC, payload, retain=True)
        attributes = dict(diagnostics or {})
        attributes["installed_ncs_version"] = payload["installed_version"]
        if release is not None:
            attributes["available_ncs_version"] = release.ncs_version
        if progress_stage is not None:
            attributes["update_stage"] = progress_stage
        self._publish_json(ATTRIBUTES_TOPIC, attributes, retain=True)
        self._publish_target_discovery(target_versions)
        self._publish_text(TARGET_STATE_TOPIC, selected_target or AUTOMATIC_TARGET, retain=True)

    def _on_connect(
        self,
        client: Any,
        userdata: Any,
        flags: Any,
        reason_code: Any,
        properties: Any = None,
    ) -> None:
        if reason_code != 0:
            LOGGER.error("MQTT connection was rejected: %s", reason_code)
            return
        self._publish_update_discovery()
        self._publish_target_discovery(())
        self._publish_manual_flash_discovery()
        client.subscribe(COMMAND_TOPIC, qos=1)
        client.subscribe(TARGET_COMMAND_TOPIC, qos=1)
        client.subscribe(MANUAL_FLASH_COMMAND_TOPIC, qos=1)
        client.publish(AVAILABILITY_TOPIC, "online", qos=1, retain=True)
        self._connected.set()

    def _on_message(self, client: Any, userdata: Any, message: Any) -> None:
        # A firmware command is an irreversible hardware action. A broker can
        # replay retained messages or redeliver QoS messages, neither of which
        # represents a new user request.
        if bool(getattr(message, "retain", False)):
            LOGGER.warning("Ignoring retained MQTT updater command on %s", message.topic)
            return
        if bool(getattr(message, "dup", False)):
            LOGGER.warning("Ignoring redelivered MQTT updater command on %s", message.topic)
            return
        if len(message.payload) > _MAX_COMMAND_BYTES:
            LOGGER.warning("Ignoring oversized MQTT update command")
            return
        try:
            command = message.payload.decode("utf-8", "strict")
        except UnicodeDecodeError:
            LOGGER.warning("Ignoring non-UTF-8 MQTT update command")
            return
        if message.topic == COMMAND_TOPIC:
            if command != INSTALL_COMMAND:
                LOGGER.warning("Ignoring unsupported MQTT update command: %r", command)
                return
            self._enqueue_command(INSTALL_COMMAND)
            return
        if message.topic == TARGET_COMMAND_TOPIC:
            if command != AUTOMATIC_TARGET and (
                not command.isascii() or not 1 <= len(command) <= 80
            ):
                LOGGER.warning("Ignoring invalid MQTT firmware target: %r", command)
                return
            self._enqueue_command(f"{SELECT_TARGET_COMMAND_PREFIX}{command}")
            return
        if message.topic == MANUAL_FLASH_COMMAND_TOPIC:
            if command != FLASH_SELECTED_COMMAND:
                LOGGER.warning("Ignoring unsupported MQTT manual-flash command: %r", command)
                return
            self._enqueue_command(FLASH_SELECTED_COMMAND)
            return
        LOGGER.warning("Ignoring MQTT command on unexpected topic: %r", message.topic)

    def _enqueue_command(self, command: str) -> None:
        """Bound MQTT input so it cannot schedule repeated hardware operations."""

        with self._command_lock:
            if self._operation_busy.is_set():
                LOGGER.warning("Ignoring MQTT command while an RCP operation is pending or active")
                return
            hardware_operation = command in _HARDWARE_OPERATION_COMMANDS
            if hardware_operation:
                self._operation_busy.set()
            try:
                self._commands.put_nowait(command)
            except Full:
                if hardware_operation:
                    self._operation_busy.clear()
                LOGGER.warning("Ignoring MQTT command because another command is pending")

    def _publish_update_discovery(self) -> None:
        self._publish_json(
            DISCOVERY_TOPIC,
            {
                "name": "nRF52840 OT RCP",
                "unique_id": "nrf52840_ot_rcp_updater_rcp",
                "device_class": "firmware",
                "entity_category": "config",
                "availability_topic": AVAILABILITY_TOPIC,
                "state_topic": STATE_TOPIC,
                "command_topic": COMMAND_TOPIC,
                "payload_install": INSTALL_COMMAND,
                "json_attributes_topic": ATTRIBUTES_TOPIC,
                "origin": DISCOVERY_ORIGIN,
                "device": self._device_info(),
            },
            retain=True,
        )

    def _publish_target_discovery(self, target_versions: tuple[str, ...]) -> None:
        self._publish_json(
            TARGET_DISCOVERY_TOPIC,
            {
                "name": "Manual RCP firmware target",
                "unique_id": "nrf52840_ot_rcp_updater_firmware_target",
                "entity_category": "config",
                "icon": "mdi:chip",
                "availability_topic": AVAILABILITY_TOPIC,
                "state_topic": TARGET_STATE_TOPIC,
                "command_topic": TARGET_COMMAND_TOPIC,
                "options": [AUTOMATIC_TARGET, *target_versions],
                "origin": DISCOVERY_ORIGIN,
                "device": self._device_info(),
            },
            retain=True,
        )

    def _publish_manual_flash_discovery(self) -> None:
        """Publish a separate action because Update entities cannot offer downgrades."""

        self._publish_json(
            MANUAL_FLASH_DISCOVERY_TOPIC,
            {
                "name": "Flash selected RCP firmware",
                "unique_id": "nrf52840_ot_rcp_updater_flash_selected_firmware",
                "icon": "mdi:upload",
                "availability_topic": AVAILABILITY_TOPIC,
                "command_topic": MANUAL_FLASH_COMMAND_TOPIC,
                "payload_press": FLASH_SELECTED_COMMAND,
                "origin": DISCOVERY_ORIGIN,
                "device": self._device_info(),
            },
            retain=True,
        )

    @staticmethod
    def _device_info() -> dict[str, object]:
        return {
            "identifiers": ["nrf52840_ot_rcp_updater"],
            "name": "nRF52840 OT RCP Updater",
            "manufacturer": "Nordic Semiconductor",
            "model": "PCA10059",
        }

    def _publish_json(self, topic: str, payload: dict[str, object], retain: bool) -> None:
        result = self._client.publish(
            topic, json.dumps(payload, separators=(",", ":")), qos=1, retain=retain
        )
        if result.rc != mqtt.MQTT_ERR_SUCCESS:
            raise MqttError(f"MQTT publish to {topic} failed with status {result.rc}")

    def _publish_text(self, topic: str, payload: str, retain: bool) -> None:
        result = self._client.publish(topic, payload, qos=1, retain=retain)
        if result.rc != mqtt.MQTT_ERR_SUCCESS:
            raise MqttError(f"MQTT publish to {topic} failed with status {result.rc}")
