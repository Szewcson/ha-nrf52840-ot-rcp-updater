from __future__ import annotations

import os
import sys
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "nrf52840_ot_rcp_updater"))

import app.main as main_module
from app.main import _load_options
from app.models import Artifact, FirmwareRelease, NcpVersion, ValidationError
from app.mqtt_update import INSTALL_COMMAND, MqttError
from app.state import StateStore


class MainOptionsTests(unittest.TestCase):
    def test_rejects_oversized_options_before_decoding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "options.json"
            path.write_bytes(b"{" + b"x" * (64 * 1024))
            with (
                patch.dict(os.environ, {"OT_RCP_OPTIONS": str(path)}, clear=False),
                self.assertRaisesRegex(ValidationError, "exceed"),
            ):
                _load_options()


class MainUpdateResultTests(unittest.TestCase):
    def test_final_mqtt_failure_keeps_a_verified_installed_state(self) -> None:
        release = FirmwareRelease(
            hardware="PCA10059",
            ncs_version="3.4.0",
            zephyr_version="4.4.0",
            dfu_application_version=3_004_000,
            artifact=Artifact(
                "https://example.invalid/rcp.elf",
                "0" * 64,
                "rcp.elf",
                "https://example.invalid/rcp.elf.sig",
            ),
            release_url="https://example.invalid/release",
            release_summary="Test release",
        )
        verified = NcpVersion(
            raw="OPENTHREAD/test; NRF52840 PCA10059 N/3.4.0 Z/4.4.0",
            hardware="PCA10059",
            ncs_version="3.4.0",
            zephyr_version="4.4.0",
        )
        handlers: dict[int, object] = {}

        class FakeUpdater:
            def __init__(self, settings: object, state_store: StateStore) -> None:
                del settings
                self._state_store = state_store

            def diagnostics(self) -> dict[str, object]:
                return {}

            def install(
                self,
                selected_release: FirmwareRelease,
                selected_target: bool,
                progress: object,
            ) -> NcpVersion:
                del selected_release, selected_target, progress
                self._state_store.save(
                    {"installed": asdict(verified), "verified_at": "2026-09-05T00:00:00+00:00"}
                )
                return verified

        class FakeMqttEntity:
            def __init__(self, *args: object, **kwargs: object) -> None:
                del args
                self._commands = kwargs["commands"]
                self._publish_count = 0

            def start(self) -> None:
                self._commands.put_nowait(INSTALL_COMMAND)

            def publish_state(self, *args: object, **kwargs: object) -> None:
                del args, kwargs
                self._publish_count += 1
                if self._publish_count == 2:
                    handler = handlers[main_module.SIGTERM]
                    assert callable(handler)
                    handler(main_module.SIGTERM, None)
                    raise MqttError("simulated final publish failure")

            def stop(self) -> None:
                pass

        def capture_signal(signum: int, handler: object) -> None:
            handlers[signum] = handler

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            options = root / "options.json"
            options.write_text(
                '{"device":"/dev/null","safe_update":false}', encoding="utf-8"
            )
            environment = {
                "OT_RCP_OPTIONS": str(options),
                "OT_RCP_STATE_DIR": str(root),
                "OT_RCP_MQTT_HOST": "mqtt",
                "OT_RCP_MQTT_PORT": "1883",
            }
            with (
                patch.dict(os.environ, environment, clear=False),
                patch("app.main.signal", side_effect=capture_signal),
                patch("app.main.MqttUpdateEntity", FakeMqttEntity),
                patch("app.main.RcpUpdater", FakeUpdater),
                patch("app.main._load_release", return_value=(object(), release, None)),
                patch("app.main._target_versions", return_value=()),
            ):
                main_module.run()

            state = StateStore(root).load()

        self.assertEqual(state["installed"]["ncs_version"], "3.4.0")
        self.assertEqual(state["installed"]["zephyr_version"], "4.4.0")


if __name__ == "__main__":
    unittest.main()
