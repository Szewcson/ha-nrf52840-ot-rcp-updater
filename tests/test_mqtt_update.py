from __future__ import annotations

from pathlib import Path
import sys
import unittest


sys.path.insert(0, str(Path(__file__).parents[1] / "nrf52840_ot_rcp_updater"))

from app.models import Artifact, FirmwareRelease
from app.mqtt_update import update_state_payload


class MqttUpdateTests(unittest.TestCase):
    def test_update_payload_uses_ncs_as_the_home_assistant_version(self) -> None:
        release = FirmwareRelease(
            hardware="PCA10059",
            ncs_version="3.3.4",
            zephyr_version="4.4.0",
            artifact=Artifact("https://example.invalid/rcp.zip", "0" * 64, "rcp.zip"),
            release_url="https://example.invalid/release",
            release_summary="Test release",
        )
        payload = update_state_payload({"ncs_version": "3.3.0"}, release, False, None)
        self.assertEqual(payload["installed_version"], "3.3.0")
        self.assertEqual(payload["latest_version"], "3.3.4")
        self.assertIn("Zephyr 4.4.0", str(payload["title"]))


if __name__ == "__main__":
    unittest.main()
