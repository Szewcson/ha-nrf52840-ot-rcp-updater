from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from queue import Empty, Queue
from threading import Event
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "nrf52840_ot_rcp_updater"))

from app import mqtt_update
from app.models import Artifact, FirmwareRelease
from app.mqtt_update import MqttUpdateEntity, update_state_payload


class _PublishResult:
    rc = 0


class _FakeMqttClient:
    def __init__(self, **kwargs: object) -> None:
        self.on_connect: object = None
        self.on_message: object = None
        self.published: list[tuple[str, object]] = []

    def will_set(self, *args: object, **kwargs: object) -> None:
        pass

    def connect_async(self, *args: object, **kwargs: object) -> None:
        pass

    def loop_start(self) -> None:
        assert self.on_connect is not None
        self.on_connect(self, None, {}, 0)

    def loop_stop(self) -> None:
        pass

    def disconnect(self) -> None:
        pass

    def subscribe(self, *args: object, **kwargs: object) -> None:
        pass

    def publish(self, topic: str, *args: object, **kwargs: object) -> _PublishResult:
        self.published.append((topic, args[0] if args else ""))
        return _PublishResult()


class _Message:
    def __init__(
        self, topic: str, payload: bytes, *, retain: bool = False, dup: bool = False
    ) -> None:
        self.topic = topic
        self.payload = payload
        self.retain = retain
        self.dup = dup


class _FakeMqtt:
    MQTT_ERR_SUCCESS = 0
    Client = _FakeMqttClient


class MqttUpdateTests(unittest.TestCase):
    def test_update_payload_uses_ncs_as_the_home_assistant_version(self) -> None:
        release = FirmwareRelease(
            hardware="PCA10059",
            ncs_version="3.3.4",
            zephyr_version="4.4.0",
            dfu_application_version=3_003_004,
            artifact=Artifact("https://example.invalid/rcp.elf", "0" * 64, "rcp.elf"),
            release_url="https://example.invalid/release",
            release_summary="Test release",
        )
        payload = update_state_payload({"ncs_version": "3.3.0"}, release, False, None)
        self.assertEqual(payload["installed_version"], "3.3.0")
        self.assertEqual(payload["latest_version"], "3.3.4")
        self.assertIn("Available firmware", str(payload["title"]))
        self.assertIn("Zephyr 4.4.0", str(payload["title"]))
        self.assertIn("Installed RCP: NCS 3.3.0", str(payload["release_summary"]))
        self.assertIsNone(payload["update_percentage"])

        progressing = update_state_payload(
            {"ncs_version": "3.3.0"},
            release,
            True,
            None,
            update_percentage=70,
            progress_stage="Flashing firmware",
        )
        self.assertEqual(progressing["update_percentage"], 70)
        self.assertIn("Update stage: Flashing firmware", str(progressing["release_summary"]))

    def test_waits_for_mqtt_connection_before_publishing_state(self) -> None:
        with patch.object(mqtt_update, "mqtt", _FakeMqtt):
            entity = MqttUpdateEntity(
                host="mqtt",
                port=1883,
                username=None,
                password=None,
                commands=Queue(maxsize=1),
                connect_timeout=0.1,
            )
            entity.start()
            entity.publish_state({}, None)

        client = entity._client
        self.assertIn(mqtt_update.DISCOVERY_TOPIC, [topic for topic, _ in client.published])
        self.assertIn(
            mqtt_update.MANUAL_FLASH_DISCOVERY_TOPIC,
            [topic for topic, _ in client.published],
        )
        self.assertIn(mqtt_update.STATE_TOPIC, [topic for topic, _ in client.published])
        update_config = json.loads(dict(client.published)[mqtt_update.DISCOVERY_TOPIC])
        self.assertEqual(update_config["device_class"], "firmware")
        self.assertEqual(update_config["entity_category"], "config")
        self.assertEqual(update_config["origin"], mqtt_update.DISCOVERY_ORIGIN)

    def test_publishes_dynamic_target_options_and_diagnostics(self) -> None:
        with patch.object(mqtt_update, "mqtt", _FakeMqtt):
            entity = MqttUpdateEntity(
                host="mqtt",
                port=1883,
                username=None,
                password=None,
                commands=Queue(maxsize=1),
                connect_timeout=0.1,
            )
            entity.start()
            entity.publish_state(
                {},
                None,
                target_versions=("3.3.4", "3.4.0"),
                selected_target="3.3.4",
                diagnostics={"dfu_target_ready": True},
            )

        published = dict(entity._client.published)
        target_config = json.loads(str(published[mqtt_update.TARGET_DISCOVERY_TOPIC]))
        self.assertEqual(target_config["options"], ["Automatic", "3.3.4", "3.4.0"])
        self.assertEqual(published[mqtt_update.TARGET_STATE_TOPIC], "3.3.4")
        self.assertEqual(
            json.loads(str(published[mqtt_update.ATTRIBUTES_TOPIC]))["dfu_target_ready"], True
        )
        attributes = json.loads(str(published[mqtt_update.ATTRIBUTES_TOPIC]))
        self.assertEqual(attributes["installed_ncs_version"], "unknown")
        self.assertNotIn("available_ncs_version", attributes)

    def test_queues_a_firmware_target_selection(self) -> None:
        commands: Queue[str] = Queue(maxsize=1)
        with patch.object(mqtt_update, "mqtt", _FakeMqtt):
            entity = MqttUpdateEntity(
                host="mqtt",
                port=1883,
                username=None,
                password=None,
                commands=commands,
                connect_timeout=0.1,
            )
            entity.start()
            entity._on_message(
                entity._client,
                None,
                _Message(mqtt_update.TARGET_COMMAND_TOPIC, b"3.4.0"),
            )

        self.assertEqual(commands.get_nowait(), "SELECT_TARGET:3.4.0")

    def test_drops_an_oversized_mqtt_command_before_decoding(self) -> None:
        commands: Queue[str] = Queue(maxsize=1)
        with patch.object(mqtt_update, "mqtt", _FakeMqtt):
            entity = MqttUpdateEntity(
                host="mqtt",
                port=1883,
                username=None,
                password=None,
                commands=commands,
                connect_timeout=0.1,
            )
            entity._on_message(
                entity._client,
                None,
                _Message(mqtt_update.COMMAND_TOPIC, b"x" * 81),
            )

        with self.assertRaises(Empty):
            commands.get_nowait()

    def test_drops_a_retained_hardware_command(self) -> None:
        commands: Queue[str] = Queue(maxsize=1)
        with patch.object(mqtt_update, "mqtt", _FakeMqtt):
            entity = MqttUpdateEntity(
                host="mqtt",
                port=1883,
                username=None,
                password=None,
                commands=commands,
                connect_timeout=0.1,
            )
            entity._on_message(
                entity._client,
                None,
                _Message(mqtt_update.COMMAND_TOPIC, b"INSTALL", retain=True),
            )

        with self.assertRaises(Empty):
            commands.get_nowait()

    def test_drops_a_redelivered_hardware_command(self) -> None:
        commands: Queue[str] = Queue(maxsize=1)
        with patch.object(mqtt_update, "mqtt", _FakeMqtt):
            entity = MqttUpdateEntity(
                host="mqtt",
                port=1883,
                username=None,
                password=None,
                commands=commands,
                connect_timeout=0.1,
            )
            entity._on_message(
                entity._client,
                None,
                _Message(mqtt_update.MANUAL_FLASH_COMMAND_TOPIC, b"FLASH_SELECTED", dup=True),
            )

        with self.assertRaises(Empty):
            commands.get_nowait()

    def test_queues_a_selected_firmware_flash(self) -> None:
        commands: Queue[str] = Queue(maxsize=1)
        with patch.object(mqtt_update, "mqtt", _FakeMqtt):
            entity = MqttUpdateEntity(
                host="mqtt",
                port=1883,
                username=None,
                password=None,
                commands=commands,
                connect_timeout=0.1,
            )
            entity.start()
            entity._on_message(
                entity._client,
                None,
                _Message(mqtt_update.MANUAL_FLASH_COMMAND_TOPIC, b"FLASH_SELECTED"),
            )

        self.assertEqual(commands.get_nowait(), mqtt_update.FLASH_SELECTED_COMMAND)

    def test_publishes_install_stage_as_an_update_attribute(self) -> None:
        with patch.object(mqtt_update, "mqtt", _FakeMqtt):
            entity = MqttUpdateEntity(
                host="mqtt",
                port=1883,
                username=None,
                password=None,
                commands=Queue(maxsize=1),
                connect_timeout=0.1,
            )
            entity.start()
            entity.publish_state(
                {},
                None,
                in_progress=True,
                update_percentage=55,
                progress_stage="Entering Secure DFU",
            )

        published = dict(entity._client.published)
        state = json.loads(str(published[mqtt_update.STATE_TOPIC]))
        attributes = json.loads(str(published[mqtt_update.ATTRIBUTES_TOPIC]))
        self.assertEqual(state["update_percentage"], 55)
        self.assertEqual(attributes["update_stage"], "Entering Secure DFU")

    def test_drops_commands_while_an_rcp_operation_is_busy(self) -> None:
        commands: Queue[str] = Queue(maxsize=1)
        operation_busy = Event()
        operation_busy.set()
        with patch.object(mqtt_update, "mqtt", _FakeMqtt):
            entity = MqttUpdateEntity(
                host="mqtt",
                port=1883,
                username=None,
                password=None,
                commands=commands,
                connect_timeout=0.1,
                operation_busy=operation_busy,
            )
            entity._on_message(
                entity._client,
                None,
                _Message(mqtt_update.COMMAND_TOPIC, b"INSTALL"),
            )

        with self.assertRaises(Empty):
            commands.get_nowait()

    def test_reserves_a_hardware_operation_before_queuing_it(self) -> None:
        commands: Queue[str] = Queue(maxsize=1)
        operation_busy = Event()
        with patch.object(mqtt_update, "mqtt", _FakeMqtt):
            entity = MqttUpdateEntity(
                host="mqtt",
                port=1883,
                username=None,
                password=None,
                commands=commands,
                connect_timeout=0.1,
                operation_busy=operation_busy,
            )
            entity._on_message(
                entity._client,
                None,
                _Message(mqtt_update.COMMAND_TOPIC, b"INSTALL"),
            )

        self.assertTrue(operation_busy.is_set())
        self.assertEqual(commands.get_nowait(), mqtt_update.INSTALL_COMMAND)

    def test_drops_a_command_when_one_is_already_pending(self) -> None:
        commands: Queue[str] = Queue(maxsize=1)
        commands.put(mqtt_update.INSTALL_COMMAND)
        with patch.object(mqtt_update, "mqtt", _FakeMqtt):
            entity = MqttUpdateEntity(
                host="mqtt",
                port=1883,
                username=None,
                password=None,
                commands=commands,
                connect_timeout=0.1,
            )
            entity._on_message(
                entity._client,
                None,
                _Message(mqtt_update.MANUAL_FLASH_COMMAND_TOPIC, b"FLASH_SELECTED"),
            )

        self.assertEqual(commands.get_nowait(), mqtt_update.INSTALL_COMMAND)


if __name__ == "__main__":
    unittest.main()
