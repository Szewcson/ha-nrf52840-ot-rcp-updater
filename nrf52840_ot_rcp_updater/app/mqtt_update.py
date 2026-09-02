"""Home Assistant MQTT Discovery transport for a native update entity."""

from __future__ import annotations

import json
import logging
from queue import SimpleQueue
from typing import Any

try:
    import paho.mqtt.client as mqtt
except ImportError:  # pragma: no cover - the container provides paho-mqtt.
    mqtt = None

from .models import FirmwareRelease


LOGGER = logging.getLogger(__name__)
DISCOVERY_TOPIC = "homeassistant/update/nrf52840_ot_rcp_updater/rcp/config"
BASE_TOPIC = "nrf52840_ot_rcp_updater/rcp"
STATE_TOPIC = f"{BASE_TOPIC}/state"
COMMAND_TOPIC = f"{BASE_TOPIC}/set"
AVAILABILITY_TOPIC = f"{BASE_TOPIC}/availability"
INSTALL_COMMAND = "INSTALL"


class MqttError(RuntimeError):
    """The Supervisor-provided MQTT service could not be used."""


def update_state_payload(
    installed: dict[str, object],
    release: FirmwareRelease | None,
    in_progress: bool,
    error: str | None,
) -> dict[str, object]:
    """Build the documented JSON state consumed by Home Assistant Update."""

    installed_ncs = installed.get("ncs_version")
    installed_version = installed_ncs if isinstance(installed_ncs, str) else "unknown"
    latest_version = release.ncs_version if release else installed_version
    summary = release.release_summary if release else "No release manifest is configured."
    if error:
        summary = f"{summary}\nLast updater error: {error}"[:255]
    title = (
        f"NCS {release.ncs_version} / Zephyr {release.zephyr_version}"
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
        "update_percentage": 0 if not in_progress else 1,
    }


class MqttUpdateEntity:
    """Publishes retained discovery/state and queues only explicit installs."""

    def __init__(
        self,
        host: str,
        port: int,
        username: str | None,
        password: str | None,
        commands: SimpleQueue[str],
    ) -> None:
        if mqtt is None:
            raise MqttError("paho-mqtt is unavailable in this app image")
        self._commands = commands
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

    def stop(self) -> None:
        self._client.publish(AVAILABILITY_TOPIC, "offline", qos=1, retain=True)
        self._client.loop_stop()
        self._client.disconnect()

    def publish_state(
        self,
        installed: dict[str, object],
        release: FirmwareRelease | None,
        in_progress: bool = False,
        error: str | None = None,
    ) -> None:
        payload = update_state_payload(installed, release, in_progress, error)
        self._publish_json(STATE_TOPIC, payload, retain=True)

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
        self._publish_json(
            DISCOVERY_TOPIC,
            {
                "name": "nRF52840 OT RCP",
                "unique_id": "nrf52840_ot_rcp_updater_rcp",
                "entity_category": "diagnostic",
                "availability_topic": AVAILABILITY_TOPIC,
                "state_topic": STATE_TOPIC,
                "command_topic": COMMAND_TOPIC,
                "payload_install": INSTALL_COMMAND,
                "device": {
                    "identifiers": ["nrf52840_ot_rcp_updater"],
                    "name": "nRF52840 OT RCP Updater",
                    "manufacturer": "Nordic Semiconductor",
                    "model": "PCA10059",
                },
            },
            retain=True,
        )
        client.subscribe(COMMAND_TOPIC, qos=1)
        client.publish(AVAILABILITY_TOPIC, "online", qos=1, retain=True)

    def _on_message(self, client: Any, userdata: Any, message: Any) -> None:
        try:
            command = message.payload.decode("utf-8", "strict")
        except UnicodeDecodeError:
            LOGGER.warning("Ignoring non-UTF-8 MQTT update command")
            return
        if command != INSTALL_COMMAND:
            LOGGER.warning("Ignoring unsupported MQTT update command: %r", command)
            return
        self._commands.put(INSTALL_COMMAND)

    def _publish_json(self, topic: str, payload: dict[str, object], retain: bool) -> None:
        result = self._client.publish(topic, json.dumps(payload, separators=(",", ":")), qos=1, retain=retain)
        if result.rc != mqtt.MQTT_ERR_SUCCESS:
            raise MqttError(f"MQTT publish to {topic} failed with status {result.rc}")
