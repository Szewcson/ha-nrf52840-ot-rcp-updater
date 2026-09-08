from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "nrf52840_ot_rcp_updater"))

from app import mqtt_update
from app.models import Artifact, FirmwareRelease
from app.mqtt_update import MqttUpdateEntity, update_state_payload
from app.operation import FlashCommand, OperationController, OperationType


class _PublishResult:
    rc = 0


class _FakeMqttClient:
    def __init__(self, **kwargs: object) -> None:
        del kwargs
        self.on_connect: object = None
        self.on_message: object = None
        self.published: list[tuple[str, object]] = []
        self.subscriptions: list[str] = []

    def will_set(self, *args: object, **kwargs: object) -> None:
        del args, kwargs

    def connect_async(self, *args: object, **kwargs: object) -> None:
        del args, kwargs

    def loop_start(self) -> None:
        assert self.on_connect is not None
        self.on_connect(self, None, {}, 0)

    def loop_stop(self) -> None:
        pass

    def disconnect(self) -> None:
        pass

    def subscribe(self, topic: str, *args: object, **kwargs: object) -> None:
        del args, kwargs
        self.subscriptions.append(topic)

    def publish(self, topic: str, *args: object, **kwargs: object) -> _PublishResult:
        del kwargs
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


def _release() -> FirmwareRelease:
    return FirmwareRelease(
        hardware="PCA10059",
        ncs_version="3.3.4",
        zephyr_version="4.4.0",
        dfu_application_version=3_003_004,
        artifact=Artifact(
            "https://example.invalid/rcp.elf",
            "0" * 64,
            "rcp.elf",
            "https://example.invalid/rcp.elf.sig",
        ),
        release_url="https://example.invalid/release",
        release_summary="Test release",
    )


class MqttUpdateTests(unittest.TestCase):
    def _entity(self, controller: OperationController) -> MqttUpdateEntity:
        return MqttUpdateEntity(
            host="mqtt",
            port=1883,
            username=None,
            password=None,
            submit_install=lambda: controller.submit(FlashCommand.install_latest()),
            connect_timeout=0.1,
        )

    def test_update_payload_uses_ncs_as_the_home_assistant_version(self) -> None:
        release = _release()
        payload = update_state_payload({"ncs_version": "3.3.0"}, release, False, None)
        self.assertEqual(payload["installed_version"], "3.3.0")
        self.assertEqual(payload["latest_version"], "3.3.4")
        self.assertIn("Available firmware", str(payload["title"]))
        self.assertIn("Zephyr 4.4.0", str(payload["title"]))
        self.assertIn("Installed RCP: NCS 3.3.0", str(payload["release_summary"]))
        self.assertIsNone(payload["update_percentage"])

    def test_publishes_native_update_and_clears_retired_helpers(self) -> None:
        with patch.object(mqtt_update, "mqtt", _FakeMqtt):
            entity = self._entity(OperationController())
            entity.start()
            entity.publish_state({}, None, diagnostics={"dfu_target_ready": True})

        client = entity._client
        published = dict(client.published)
        self.assertIn(mqtt_update.DISCOVERY_TOPIC, published)
        self.assertEqual(published[mqtt_update.LEGACY_TARGET_DISCOVERY_TOPIC], "")
        self.assertEqual(published[mqtt_update.LEGACY_MANUAL_FLASH_DISCOVERY_TOPIC], "")
        self.assertEqual(client.subscriptions, [mqtt_update.COMMAND_TOPIC])
        update_config = json.loads(str(published[mqtt_update.DISCOVERY_TOPIC]))
        self.assertEqual(update_config["name"], "PCA10059 OpenThread RCP")
        self.assertEqual(update_config["device_class"], "firmware")
        self.assertEqual(update_config["origin"], mqtt_update.DISCOVERY_ORIGIN)
        self.assertEqual(update_config["device"]["name"], "PCA10059 OpenThread RCP Updater")
        attributes = json.loads(str(published[mqtt_update.ATTRIBUTES_TOPIC]))
        self.assertTrue(attributes["dfu_target_ready"])
        self.assertEqual(attributes["installed_ncs_version"], "unknown")

    def test_mqtt_install_uses_the_shared_operation_controller(self) -> None:
        controller = OperationController()
        with patch.object(mqtt_update, "mqtt", _FakeMqtt):
            entity = self._entity(controller)
            entity._on_message(entity._client, None, _Message(mqtt_update.COMMAND_TOPIC, b"INSTALL"))

        operation = controller.next(0)
        assert operation is not None
        self.assertIs(operation.command.operation_type, OperationType.INSTALL_LATEST)
        self.assertEqual(operation.command.source, "mqtt")

    def test_drops_retained_redelivered_and_oversized_commands(self) -> None:
        controller = OperationController()
        with patch.object(mqtt_update, "mqtt", _FakeMqtt):
            entity = self._entity(controller)
            entity._on_message(
                entity._client, None, _Message(mqtt_update.COMMAND_TOPIC, b"INSTALL", retain=True)
            )
            entity._on_message(
                entity._client, None, _Message(mqtt_update.COMMAND_TOPIC, b"INSTALL", dup=True)
            )
            entity._on_message(entity._client, None, _Message(mqtt_update.COMMAND_TOPIC, b"x" * 81))

        self.assertIsNone(controller.next(0))

    def test_busy_controller_rejects_a_second_mqtt_install(self) -> None:
        controller = OperationController()
        controller.submit(FlashCommand.install_latest())
        with patch.object(mqtt_update, "mqtt", _FakeMqtt):
            entity = self._entity(controller)
            entity._on_message(entity._client, None, _Message(mqtt_update.COMMAND_TOPIC, b"INSTALL"))

        operation = controller.next(0)
        assert operation is not None
        self.assertEqual(operation.command.source, "mqtt")
        self.assertIsNone(controller.next(0))

    def test_publishes_install_stage_as_an_update_attribute(self) -> None:
        with patch.object(mqtt_update, "mqtt", _FakeMqtt):
            entity = self._entity(OperationController())
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


if __name__ == "__main__":
    unittest.main()
